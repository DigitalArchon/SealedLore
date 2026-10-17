"""Map-first play (engine/area.py, engine/furnish.py, engine/tile_sets.py,
engine/tile_draw.py, engine/session_area.py; SealedLoreDocs design/map-first.md):
the law layer, its rules, walking, standing, what the storyteller is told,
and the turn's walking carried into the story and the plot's clock."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from sealedlore.engine.area import (
    area_of,
    build_area,
    furnish,
    item_verbs,
    path,
    render_positions,
    stand_near,
    walk_metres,
    walk_seconds,
)
from sealedlore.engine.furnish import furnish_messages, parse_furnishing
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tile_draw import (
    item_jobs,
    parse_tile_words,
    tile_jobs,
    tile_set_price,
    walker_jobs,
)
from sealedlore.engine.tile_sets import TileSetInfo, choose_set, item_slugs, own_key
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.world_art import rasterise
from sealedlore.models.area import FurnishItem, FurnishPlan, FurnishRoom, Spot
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.world import (
    OwnTileSet,
    PlaceLayout,
    WorldArt,
    WorldDelta,
    WorldMap,
    WorldPlace,
    WorldThing,
)
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from sealedlore.storage.world_art import save_world_art

# The farm: a 40 x 24 plan at 10 m a tile, a road along it, the house fixed
# where the map has it (the program draws it, with its door).
FARM_SPEC = {
    "ground": "grass",
    "features": [
        {"kind": "road", "points": [[0, 20], [39, 20]], "width": 1},
        {"kind": "road", "points": [[22, 0], [22, 23]], "width": 1},  # runs under the house
        {"kind": "object", "name": "a stone well", "x": 30, "y": 6},
    ],
}
# The house's inside: two rooms with no door between them (the back room is
# sealed), a gap of 12 tiles lost from the north wall, a stove.
HOUSE_SPEC = {
    "ground": "floor",
    "features": [
        {
            "kind": "building",
            "x": 0,
            "y": 0,
            "w": 40,
            "h": 24,
            "doors": [{"side": "south", "at": 10}],
        },
        {"kind": "wall", "points": [[20, 0], [20, 23]]},
        {"kind": "area", "fill": "floor", "x": 4, "y": 0, "w": 12, "h": 1},
        {"kind": "object", "name": "a cast-iron stove", "x": 25, "y": 3},
    ],
}


def world_and_art(house_objects: Sequence[dict] = ()) -> tuple[WorldMap, WorldArt]:
    """The farm and its house; `house_objects` are more objects for the
    house's plan ({"kind": "object", "name": ..., "x": ..., "y": ...})."""
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400.0)
    house = WorldPlace(
        id="house", name="The house", kind="building", inside="farm", x=20.0, y=30.0, size=40.0
    )
    town = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=8000.0, size=1500.0)
    world = WorldMap(
        places={"farm": farm, "house": house, "town": town}, seen=["farm", "house", "town"]
    )
    art = WorldArt()
    rows, objects, labels = rasterise(FARM_SPEC, 40, 24)
    art.layouts["farm"] = {
        "": PlaceLayout(place_id="farm", spec=FARM_SPEC, rows=rows, objects=objects, made_at="1")
    }
    house_spec = {**HOUSE_SPEC, "features": [*HOUSE_SPEC["features"], *house_objects]}
    rows, objects, labels = rasterise(house_spec, 40, 24)
    art.layouts["house"] = {
        "": PlaceLayout(
            place_id="house",
            spec=house_spec,
            rows=rows,
            objects=objects,
            labels=labels,
            made_at="1",
        )
    }
    return world, art


def reachable_from(area, start) -> set:
    seen, todo = set(), [start]
    while todo:
        x, y = todo.pop()
        if (x, y) in seen or not area.walkable(x, y):
            continue
        seen.add((x, y))
        todo += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    return seen


# --- the law layer --------------------------------------------------------------


def test_a_building_keeps_its_rules_every_room_reached_and_its_wall_whole():
    world, art = world_and_art()
    house = build_area(world, art, "house")
    assert house.indoor and house.tile_m == pytest.approx(1.0)
    # The back room had no door: one is opened in the wall between.
    assert any("sealed" in r for r in house.repairs)
    # The 12-tile gap in the north wall is wall again, but for a doorway.
    assert any("walled a gap of 12" in r for r in house.repairs)
    assert sum(1 for x in range(40) if house.rows[0][x] == "_") == 3
    door = next(e for e in house.exits if e[1] == 23)
    inside = reachable_from(house, (door[0], door[1] - 1))
    floor = {(x, y) for y, row in enumerate(house.rows) for x, ch in enumerate(row) if ch == "_"}
    assert floor <= inside | {t for t in floor if t[1] == 0}  # every floor tile is reached
    assert len(house.rooms) == 2


def test_outdoors_the_plan_is_walking_scale_and_its_road_never_runs_through_a_building():
    world, art = world_and_art()
    farm = build_area(world, art, "farm")
    assert (farm.width, farm.height) == (40 * 3, 24 * 3)  # 10 m tiles scaled by 3
    # A tile is as wide as the map says (walking takes as long as it is far).
    assert not farm.indoor and farm.tile_m == pytest.approx(10 / 3)
    x0, y0, x1, y1 = farm.buildings["house"]
    inside = [farm.rows[y][x] for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
    assert "=" not in inside
    # The map's house has its door, and it leads into the house's own area.
    assert [e.place_id for e in farm.entrances] == ["house"]
    well = next(p for p in farm.placements if p.name == "a stone well")
    assert well.kind == "object" and "use" in well.verbs and "take" not in well.verbs


def test_someone_at_a_place_stands_in_the_area_round_them():
    world, art = world_and_art()
    assert area_of(world, art, "house") == "house"  # it has an inside of its own
    assert area_of(world, art, "farm") == "farm"
    # No plan drawn yet: a window of the open country there (the author:
    # the world is limitless), named by its rectangle on the map.
    assert area_of(world, art, "town") == "window:0:9600:1600"


def test_furniture_keeps_doorways_and_walkways_clear_and_cuts_nothing_off():
    world, art = world_and_art()
    house = build_area(world, art, "house")
    plan = FurnishPlan(
        place_id="house",
        rooms=[
            FurnishRoom(
                room=r.index,
                purpose="kitchen",
                items=[
                    FurnishItem(name="a pine table", w=3, h=2, where="middle", count=2),
                    FurnishItem(name="a wooden shelf", w=2, h=1, where="wall", count=6),
                    FurnishItem(name="a barrel", where="corner", count=4),
                ],
            )
            for r in house.rooms
        ],
    )
    counts = {d: len(furnish(house, plan, d)) for d in ("sparse", "normal", "full")}
    assert counts["sparse"] < counts["normal"] < counts["full"]
    pieces = furnish(house, plan, "full")
    taken = {t for p in pieces for t in p.tiles()}
    for d in house.doorways:
        for x, y in d.tiles():
            assert (x, y) not in taken
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                assert (x + dx, y + dy) not in taken  # the doorway's front is clear
    house.placements += pieces
    door = next(e for e in house.exits if e[1] == 23)
    reached = reachable_from(house, (door[0], door[1] - 1))
    for p in pieces:  # every piece can be walked up to
        assert any(
            (x + dx, y + dy) in reached
            for x, y in p.tiles()
            for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0))
        )
    assert furnish(house, plan, "full") == pieces  # the same every time


def test_walking_goes_round_walls_and_things_and_takes_little_time():
    world, art = world_and_art()
    house = build_area(world, art, "house")
    door = next(e for e in house.exits if e[1] == 23)
    stove = next(p for p in house.placements if "stove" in p.name)
    start = (door[0], door[1] - 1)
    steps = path(house, start, (5, 5))
    assert steps[0] == start and steps[-1] == (5, 5)
    assert all(house.walkable(*s) for s in steps)
    assert (stove.x, stove.y) not in steps
    for (ax, ay), (bx, by) in zip(steps, steps[1:], strict=False):
        assert max(abs(bx - ax), abs(by - ay)) == 1
        if ax != bx and ay != by:  # never across a corner
            assert house.walkable(bx, ay) and house.walkable(ax, by)
    metres = walk_metres(house, steps)
    assert 15 < metres < 40 and walk_seconds(metres) < 30
    assert path(house, start, (0, 0)) == []  # a wall


def test_standing_near_a_spot_never_stands_on_a_wall_a_thing_or_someone_else():
    world, art = world_and_art()
    house = build_area(world, art, "house")
    stove = next(p for p in house.placements if "stove" in p.name)
    spot = stand_near(house, (stove.x, stove.y), {(stove.x, stove.y + 1)})
    assert spot != (stove.x, stove.y + 1) and house.walkable(*spot)
    assert stand_near(house, (0, 0)) != (0, 0)


def test_the_storyteller_is_told_where_everyone_stands_and_never_the_day():
    world, art = world_and_art()
    house = build_area(world, art, "house")
    stove = next(p for p in house.placements if "stove" in p.name)
    spots = {
        "Jane": Spot(area="house", x=stove.x, y=stove.y + 1),
        "Marcus": Spot(area="house", x=stove.x + 4, y=stove.y + 1),
        "Ben": Spot(area="farm", x=3, y=3),
    }
    text = render_positions(house, world, spots, "Jane", walked="about 2 minutes on foot")
    assert "- Jane: in The house, in the smaller central hall, by the cast-iron stove." in text
    assert "- Marcus: 4 m east of Jane" in text
    assert "; in sight, within earshot." in text
    assert "Ben" not in text  # not in this area
    assert "Within reach: the cast-iron stove" in text
    assert "Since the last passage: about 2 minutes on foot." in text
    assert "Day" not in text and "day" not in text


def test_what_can_be_done_with_a_thing_comes_from_its_plan_or_its_name():
    assert item_verbs("a loaf of bread") == ("use", "take", "eat", "destroy")
    assert item_verbs("a heavy oak dining table") == ("use", "destroy")
    assert item_verbs("an oil lamp") == ("use", "take", "destroy")
    assert item_verbs("a cast-iron stove", ["use", "take"]) == ("use", "destroy")


def test_a_furnishing_reply_is_held_to_the_building_it_was_asked_for():
    world, art = world_and_art()
    house = build_area(world, art, "house")
    messages = furnish_messages(house, world.places["house"], "A snowbound farm.")
    assert "Room 0: the larger central hall" in messages[-1].text
    reply = json.dumps(
        {
            "rooms": [
                {
                    "room": 0,
                    "purpose": "kitchen",
                    "items": [
                        {
                            "name": "a pine table",
                            "w": 9,
                            "h": 0,
                            "where": "nowhere",
                            "count": 40,
                            "verbs": ["use", "fly"],
                        },
                    ],
                },
                {"room": 7, "purpose": "ghost", "items": [{"name": "a ghost"}]},
            ]
        }
    )
    plan = parse_furnishing(reply, house, place_id="house", state="", model="m", made_at="t")
    assert [r.room for r in plan.rooms] == [0]
    item = plan.rooms[0].items[0]
    assert (item.w, item.h, item.where, item.count, item.verbs) == (4, 1, "wall", 8, ["use"])
    with pytest.raises(ValueError):
        parse_furnishing('{"rooms": []}', house, place_id="house", state="", model="m", made_at="t")


# --- tile sets and what is drawn ---------------------------------------------------


def test_a_tile_set_is_chosen_by_what_the_setting_says():
    shipped = {
        "farm": TileSetInfo(name="farm", keywords=["farm", "snow"]),
        "elf": TileSetInfo(name="elf", keywords=["elf", "forest"]),
        "station": TileSetInfo(name="station", keywords=["space"], deck=True),
    }
    assert choose_set("", "An elven forest at the edge of the world.", [], shipped) == "elf"
    assert choose_set("", "Deep Nine, a station.", ["space"], shipped) == "station"
    assert choose_set("farm", "An elven forest.", [], shipped) == "farm"  # the author's choice
    # One of the story's own, while it is there; gone, the setting decides.
    assert choose_set("own:neon", "", [], shipped, own=["neon"]) == "own:neon"
    assert choose_set("own", "", [], shipped, own=["own"]) == "own"  # the old single set
    assert choose_set("own:gone", "An elven forest.", [], shipped, own=["neon"]) == "elf"
    assert choose_set("", "Nothing in particular.", [], shipped) == "elf"  # the first, by name
    assert item_slugs("A wooden chest") == ["a-wooden-chest", "wooden-chest"]
    assert own_key("Rain-slick alleys", []) == "rain-slick-alleys"
    assert own_key("Rain-slick alleys", ["rain-slick-alleys"]) == "rain-slick-alleys-2"
    assert own_key("", ["own"]) == "own-2"


def test_sprites_are_drawn_for_what_has_none_and_people_from_their_portraits():
    world, art = world_and_art()
    farm = build_area(world, art, "farm")
    jobs = item_jobs([farm, farm], lambda name: False, "a snowbound farm")
    assert [j.key for j in jobs] == ["item:a-stone-well"]
    assert "a stone well, as found in a snowbound farm" in jobs[0].prompt
    assert item_jobs([farm], lambda name: True, "x") == []
    people = [("Jane", "images/refs/jane.jpg", ""), ("Ben", None, "a tall farmer in a red coat")]
    walkers = walker_jobs(people, lambda name: False, "a snowbound farm")
    assert walkers[0].reference == "images/refs/jane.jpg" and "image 1" in walkers[0].prompt
    assert walkers[1].reference is None and "Ben, a tall farmer in a red coat" in walkers[1].prompt


def test_a_story_s_own_tile_set_is_drawn_from_words_written_for_its_world():
    words = {
        "world": "a drowned city",
        "floor": "wet planks",
        "cap": "slate",
        "face": "slate wall",
        "rug": "a reed mat",
        "decor": ["a lamp", "a net", "a jar"],
        "out-ground": "mud",
        "out-road": "planks",
        "out-water": "green water",
        "out-pave": "stone",
        "out-canopy": "reeds",
        "out-roof": "thatch",
        "out-wall": "brick",
        "trees": ["a willow", "a dead tree"],
        "out-bush": "reeds",
        "out-rock": "a rock",
        "out-rubble": "bricks",
        "out-fence": "a rail",
        "out-door": "a door",
        "light": [200, 220, 255],
        "lava": False,
    }
    parsed = parse_tile_words(json.dumps(words))
    assert parsed["decor1"] == "a net" and parsed["out-tree1"] == "a dead tree"
    assert parsed["light"] == "200,220,255"
    jobs = tile_jobs(parsed)
    roles = [role for role, _prompt, _size in jobs]
    assert roles[:3] == ["floor", "cap", "face"] and "out-door" in roles
    assert all("a drowned city" in prompt for _role, prompt, _size in jobs)
    with pytest.raises(ValueError):
        parse_tile_words(json.dumps({"world": "x"}))
    # The designer's own prompt for a picture is kept with the words and
    # sent for that picture, the template for the rest; one that forgets the
    # style gets it, so the set reads as one; a prompt for nothing drawn, or
    # for no role, is dropped.
    designed = {
        **words,
        "style": "bleak and cold, muted greys",
        "prompts": {
            "floor": "Pixel art, bleak and cold, muted greys: wet planks of a drowned city.",
            "out-door": "A warped door  of a drowned city, on white.",
            "out-canopy": "reeds",
            "rug": "",
            "nothing": "x",
        },
    }
    parsed = parse_tile_words(json.dumps({**designed, "out-canopy": ""}))
    assert parsed["prompt:floor"] == designed["prompts"]["floor"]
    assert parsed["prompt:out-door"] == (
        "A warped door of a drowned city, on white. Style: bleak and cold, muted greys."
    )
    assert "prompt:rug" not in parsed and "prompt:nothing" not in parsed
    assert "prompt:out-canopy" not in parsed  # nothing to draw it for
    sent = {role: prompt for role, prompt, _size in tile_jobs(parsed)}
    assert sent["floor"] == parsed["prompt:floor"]
    assert sent["out-door"] == parsed["prompt:out-door"]
    assert "wet planks" not in sent["cap"] and "slate, in a drowned city" in sent["cap"]
    # The whole set's price is each picture at its size, never a guess.
    prices = {"1024x1024": 0.01, "768x768": 0.005}
    jobs = tile_jobs(parsed)
    textures = sum(1 for _r, _p, size in jobs if size == "1024x1024")
    assert tile_set_price(jobs, prices.get) == pytest.approx(
        textures * 0.01 + (len(jobs) - textures) * 0.005
    )
    assert tile_set_price(jobs, lambda _size: None) is None


# --- the session ---------------------------------------------------------------------


def make_session(
    tmp_path: Path,
    story,
    cast,
    responses=(),
    *,
    plot=None,
    house_objects: Sequence[dict] = (),
    things: Sequence[WorldThing] = (),
) -> StorySession:
    world, art = world_and_art(house_objects)
    story.defaults.world_map = True
    story.world_base = WorldDelta(
        places=list(world.places.values()), seen=list(world.seen), things=list(things)
    )
    story.world_start = "house"
    story.held_character_id = "char-serrik"
    story.plot = plot
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    save_world_art(story.id, art, tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        min_cacheable_tokens=1,
        scene_model="small/scene-model",
        scene_reads="manual",
        map_art=False,
    )
    return StorySession(
        bundle,
        config,
        MockChatProvider(list(responses)),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn(text: str) -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text=text, controlled_character_id="char-serrik"
    )


def test_the_author_s_character_starts_inside_and_walks_out_through_the_door(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    assert me.area == "house"
    house = session.area("house")
    door = next(e for e in house.exits if e[1] == 23)
    steps = session.walk_route(*door)
    assert steps and steps[-1] == door
    state = session.walked(steps)
    assert state.walked > 0 and session.story.area_live is not None
    assert session.door_here() == ("farm", "The farm")
    state = session.go_through()
    me = state.spots["Serrik Vaun"]
    farm = session.area("farm")
    assert me.area == "farm"
    entrance = farm.entrances[0]
    assert abs(me.x - entrance.x) + abs(me.y - entrance.y) <= 2  # outside its door
    assert "from The house to The farm" in state.walked_words


def test_a_turn_carries_the_walking_and_the_storyteller_is_told_where_everyone_is(
    tmp_path, story, cast
):
    session = make_session(tmp_path, story, cast, ["He stood in the yard."])
    house = session.area("house")
    door = next(e for e in house.exits if e[1] == 23)
    session.walked(session.walk_route(*door))
    session.go_through()
    list(session.send(turn("I look at the sky.")))
    user = session.path()[-2]
    assert user.meta.area is not None and user.meta.area.spots["Serrik Vaun"].area == "farm"
    assert user.meta.world is not None and user.meta.world.where == {"Serrik Vaun": "farm"}
    assert session.story.area_live is None
    assert session.world().where["Serrik Vaun"] == "farm"
    sent = session.provider.requests[0].messages[-1].text
    assert "Where everyone stands (the map is exact: keep to it):" in sent
    assert "- Serrik Vaun: in The farm" in sent
    # A walk of a few seconds isn't worth a line; one of half a minute or more is.
    assert "Since the last passage" not in sent


def test_a_menu_choice_writes_its_turn_and_says_what_it_means_only_as_written(
    tmp_path, story, cast
):
    session = make_session(tmp_path, story, cast, ["She looks up.", "Nothing."])
    stove = next(p for p in session.area("house").placements if "stove" in p.name)
    text = session.map_action("use", stove.name, stove.id)
    assert text == "I use the cast-iron stove."
    list(session.send(turn(text + " It is cold.")))
    sent = session.provider.requests[0].messages[-1].text
    assert "The author chose Use on the map" in sent
    session.map_action("talk", "Maela Orr")
    list(session.send(turn("I keep walking.")))  # a turn of the author's own
    sent = session.provider.requests[-1].messages[-1].text
    assert "The author chose" not in sent


def test_travelling_moves_the_clock_by_the_way_s_length(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    choices = {name: seconds for _pid, name, seconds in session.travel_choices()}
    assert "The farm" not in choices and "The house" not in choices
    from sealedlore.engine.area import WALK_M_PER_S
    from sealedlore.engine.session_area import WINDING

    straight = 8000.0 - 30.0  # from the house's spot to Hellsville's
    expected = straight * WINDING / WALK_M_PER_S
    assert abs(choices["Hellsville"] - expected) < expected * 0.05  # winding, on foot
    text = session.travel("town")
    assert text == "I set off for Hellsville."
    state = session.area_state()
    # Nothing moves until the turn is sent: a turn rewritten travels nowhere.
    assert state.walked == 0 and "Serrik Vaun" in state.spots
    assert state.action.verb == "travel" and state.action.seconds == choices["Hellsville"]
    list(session.send(turn("I stay where I am.")))
    assert session.world().where["Serrik Vaun"] == "house"
    assert session.path()[-2].meta.area.walked == 0
    text = session.travel("town")
    list(session.send(turn(text)))
    stamped = session.path()[-2].meta.area
    assert stamped.walked == choices["Hellsville"] and "to Hellsville" in stamped.walked_words
    # Hellsville has no plan yet: they stand on the open country at its door.
    assert stamped.spots["Serrik Vaun"].area.startswith("window:")
    assert session.world().where["Serrik Vaun"] == "town"


def test_the_read_may_stand_someone_at_a_spot_and_tell_the_time_that_passed(tmp_path, story, cast):
    read = json.dumps(
        {
            "where": [{"who": "Maela", "place": "The house", "quote": "Maela came in"}],
            "at": {"Maela": "the cast-iron stove"},
            "minutes": 60,
            "minutes_quote": "An hour later",
        }
    )
    session = make_session(tmp_path, story, cast, ["Maela came in. An hour later, tea.", read])
    session.config.scene_reads = "manual"
    list(session.send(turn("I wait.")))
    passage = session.path()[-1]
    state = passage.meta.area
    assert state is not None
    stove = next(p for p in session.area("house").placements if "stove" in p.name)
    maela = state.spots["Maela Orr"]
    assert abs(maela.x - stove.x) + abs(maela.y - stove.y) <= 2
    # The hour told, on the morning the opening starts at when it names no time.
    from sealedlore.engine.session_area import DEFAULT_START_MINUTE

    assert state.seconds == DEFAULT_START_MINUTE * 60 + 3600
    spots = session.provider.requests[-1].messages[-1].text
    line = next(x for x in spots.splitlines() if x.startswith("SPOTS NEAR THE AUTHOR'S CHARACTER:"))
    assert "the cast-iron stove" in line


def test_walking_moves_the_plot_s_clock_as_the_author_s_skip_does():
    from sealedlore.engine.session_area import AreaRuntime
    from sealedlore.models.area import AreaState
    from sealedlore.models.node import Node, NodeMeta
    from sealedlore.models.plot import Chronicle

    node = Node(
        kind="user", speaker_id="x", content="On.", meta=NodeMeta(area=AreaState(walked=1260))
    )
    chronicle = Chronicle(minutes=600)

    assert AreaRuntime.walk_into_plot(None, node, chronicle) == 21
    assert (chronicle.minutes, chronicle.elapsed) == (621, 21)
    assert (
        AreaRuntime.walk_into_plot(None, Node(kind="user", speaker_id="x", content=""), chronicle)
        == 0
    )


def test_the_ground_s_cover_never_cuts_any_ground_off():
    world, art = world_and_art()
    farm = build_area(world, art, "farm")
    entrance = farm.entrances[0]
    reached = reachable_from(farm, (entrance.x, entrance.y + 1))
    ground = {
        (x, y)
        for y, row in enumerate(farm.rows)
        for x, ch in enumerate(row)
        if farm.walkable(x, y) and ch != "+"
    }
    assert ground - reached == set()
    assert sum(row.count("T") for row in farm.rows) > 100  # still a wooded farm


def test_walls_stop_sight_and_the_storyteller_is_told_who_is_out_of_sight():
    from sealedlore.engine.area import in_earshot, seen_tiles, sees

    world, art = world_and_art()
    house = build_area(world, art, "house")
    jane, marcus, ben = (5, 10), (30, 10), (15, 5)
    assert not sees(house, jane, marcus) and sees(house, jane, ben)
    assert sees(house, (5, 12), (30, 12))  # through the doorway in the wall
    assert in_earshot(house, jane, ben) and not in_earshot(house, jane, marcus)
    grid = seen_tiles(house, jane)
    assert grid[10][15] and not grid[10][30]
    spots = {
        "Jane": Spot(area="house", x=jane[0], y=jane[1]),
        "Marcus": Spot(area="house", x=marcus[0], y=marcus[1]),
    }
    text = render_positions(house, world, spots, "Jane")
    assert "(another room); out of sight." in text


def _stand(session, area_id: str, x: int, y: int) -> None:
    from sealedlore.models.area import LiveArea

    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area=area_id, x=x, y=y)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)


def test_at_an_area_s_edge_the_places_that_way_are_offered(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    _stand(session, "farm", 30, 0)
    assert session.edge_here() == "north"
    choices = session.edge_choices("north")
    # The open country past the edge (a window of the land, drawn as the
    # story goes there: tests/test_open_country.py), then the places that way.
    assert [c[1] for c in choices] == ["open country", "Hellsville"]  # 8 km north
    assert session.edge_choices("south") == []  # the far side: still the farm
    state = session.cross_edge("north")  # open country adjoins everywhere
    assert state.spots["Serrik Vaun"].area.startswith("window:")


def test_an_area_that_adjoins_is_walked_on_into(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    # A meadow whose plan starts 5 m past the farm's north edge.
    meadow = WorldPlace(id="meadow", name="The meadow", kind="site", x=0.0, y=245.0, size=400.0)
    session.story.world_base.places.append(meadow)
    session.story.world_base.seen.append("meadow")
    rows, objects, _labels = rasterise({"ground": "grass", "features": []}, 40, 24)
    session.world_art.layouts["meadow"] = {
        "": PlaceLayout(
            place_id="meadow", spec={"ground": "grass", "features": []}, rows=rows, made_at="1"
        )
    }
    _stand(session, "farm", 30, 0)
    choices = session.edge_choices("north")
    assert choices[0][:2] == ("area", "The meadow")
    assert [c[1] for c in choices].count("The meadow") == 1  # once: as the walk on
    state = session.cross_edge("north")
    me = state.spots["Serrik Vaun"]
    assert me.area == "meadow" and me.y == session.area("meadow").height - 1
    assert 0 < state.walked < 30
    # A river along the meadow's south edge bars the way: no crossing for free.
    wet = {
        "ground": "grass",
        "features": [{"kind": "area", "fill": "water", "x": 0, "y": 20, "w": 40, "h": 4}],
    }
    rows, objects, _labels = rasterise(wet, 40, 24)
    session.world_art.layouts["meadow"] = {
        "": PlaceLayout(place_id="meadow", spec=wet, rows=rows, made_at="2")
    }
    session._area_cache = None
    _stand(session, "farm", 30, 0)
    assert session.cross_edge("north") is None
    assert all(c[0] != "area" for c in session.edge_choices("north"))


def test_one_asset_of_the_tile_set_is_changed_drawn_again_or_put_back(
    tmp_path, story, cast, monkeypatch
):
    from sealedlore.engine import session_area
    from tests.test_world_pictures import FakeImages, png

    FakeImages.sent = []
    monkeypatch.setattr(session_area, "ImageClient", FakeImages)
    session = make_session(tmp_path, story, cast)
    session.config.providers[0].api_key = "k"
    assets = {a.role: a for a in session.tile_set_assets()}
    assert {"floor", "out-road", "walker/Serrik Vaun"} <= set(assets)
    assert any(role.startswith("items/") for role in assets)
    assert assets["floor"].source == "the set's"
    shipped_floor = session.tile_asset_bytes("floor")
    # A picture of the author's own, then back to the set's.
    session.use_own_tile_asset("floor", png(16, 16))
    assert session.tile_asset_bytes("floor") != shipped_floor
    assets = {a.role: a for a in session.tile_set_assets()}
    assert assets["floor"].source == "changed for this story"
    session.reset_tile_asset("floor")
    assert session.tile_asset_bytes("floor") == shipped_floor
    # Drawn again from the author's words: the map takes it once it is in.
    model, _price, problem = session.tile_asset_offer("out-road", "wet cobbles")
    assert problem is None and model == session.config.map_tile_model
    assert session.redraw_tile_asset("out-road", "wet cobbles")
    session._asset_thread.join(5)
    sent = FakeImages.sent[-1]
    assert "wet cobbles" in sent["prompt"] and sent["enable_prompt_expansion"] is False
    assert session.map_art_copy().tile_overrides["out-road"].startswith("images/map/tileset/")
    # A walker of the author's own stands in for the drawn one.
    session.use_own_tile_asset("walker/Serrik Vaun", png(12, 20))
    assert session.walker_file("Serrik Vaun").startswith("images/map/tileset/")


def test_a_story_s_own_tile_sets_and_its_changes_go_into_a_new_playthrough(tmp_path):
    from sealedlore.storage.world_art import art_with_files, carried_art, load_sidecar

    art = WorldArt(
        tile_set="own:neon",
        tile_sets={
            "neon": OwnTileSet(
                title="Neon",
                assets={"floor": "images/map/tileset/floor-1.png", "cap": "images/map/gone.png"},
                words={"world": "a city", "style": "neon"},
            ),
            "lost": OwnTileSet(title="Lost", assets={"floor": "images/map/lost.png"}),
        },
        tile_overrides={"out-road": "images/map/tileset/road-1.png", "rug": ""},
    )
    files = {"images/map/tileset/floor-1.png": b"F", "images/map/tileset/road-1.png": b"R"}
    carried, out = carried_art(art, files.get)
    # Each asset only with its file; a set left with none is left out.
    assert carried.tile_sets["neon"].assets == {"floor": "images/map/tileset/floor-1.png"}
    assert carried.tile_sets["neon"].words == {"world": "a city", "style": "neon"}
    assert "lost" not in carried.tile_sets and carried.tile_set == "own:neon"
    assert carried.tile_overrides == {"out-road": "images/map/tileset/road-1.png", "rug": ""}
    assert out == files
    # A scenario brings the same, by the files it carries.
    kept = art_with_files(art, files.__contains__)
    assert list(kept.tile_sets) == ["neon"] and kept.tile_overrides == carried.tile_overrides
    # A sample's sidecar never names files the new story won't have.
    plot = tmp_path / "Sample.md"
    plot.write_text("# x", encoding="utf-8")
    (tmp_path / "Sample.world_art.json").write_text(art.model_dump_json(), encoding="utf-8")
    shipped, _files = load_sidecar(plot)
    assert shipped is not None and shipped.tile_sets == {} and shipped.tile_set == ""
    assert shipped.tile_overrides == {"rug": ""}


def test_a_story_may_keep_several_own_tile_sets_and_the_one_it_had_is_read():
    """The one own set a story could keep before (`tile_assets`, `tile_words`,
    `tile_set == "own"`) opens as the first of several."""
    old = {
        "tile_set": "own",
        "tile_assets": {"floor": "images/map/tileset/floor-1.png"},
        "tile_words": {"world": "a drowned city", "style": "bleak"},
    }
    art = WorldArt.model_validate(old)
    assert art.tile_set == "own:own" and list(art.tile_sets) == ["own"]
    assert art.tile_sets["own"].assets == old["tile_assets"]
    assert art.tile_sets["own"].words == old["tile_words"]
    assert art.tile_sets["own"].title == "This story's own"
    assert art.own_set_for("own:own") == ("own", art.tile_sets["own"])
    assert art.own_set_for("own") == ("own", art.tile_sets["own"])  # the old value still reads
    assert art.own_set_for("doomsville") is None and art.own_set_for("own:gone") is None
    dumped = art.model_dump()
    assert "tile_assets" not in dumped and "tile_words" not in dumped
    assert WorldArt.model_validate(dumped) == art
    # Nothing drawn before: no set, the choice left alone.
    empty = WorldArt.model_validate({"tile_set": "doomsville", "tile_assets": {}, "tile_words": {}})
    assert empty.tile_sets == {} and empty.tile_set == "doomsville"
    # Several, each with its own words, swapped between by name.
    art.tile_sets["neon"] = OwnTileSet(
        title="Neon alleys", assets={"floor": "images/map/tileset/f-2.png"}, words={"style": "neon"}
    )
    art.tile_set = "own:neon"
    assert art.own_set_for(art.tile_set)[1].words == {"style": "neon"}


def test_a_set_is_designed_from_a_description_shown_with_its_price_and_drawn_as_approved(
    tmp_path, story, cast, monkeypatch
):
    """The author's ruling (Oct 16 2026): show the sets there are, offer to
    draw new ones, have the author describe what they want and a designer
    model manage the pictures. Nothing is drawn until the author has seen
    every prompt and the price; what they approve is what is sent."""
    from sealedlore.engine import session_area
    from sealedlore.storage.repository import read_api_log
    from tests.test_world_pictures import FakeImages

    design = {
        "world": "rain-slick cyberpunk alleys",
        "style": "gritty neon noir, wet concrete and pink light",
        "floor": "cracked concrete",
        "cap": "a steel wall top",
        "face": "a graffitied concrete wall",
        "rug": "a soaked cardboard mat",
        "decor": ["a vending machine", "a neon sign"],
        "out-ground": "wet asphalt",
        "out-road": "a rain-slick street",
        "out-wall": "a concrete facade with neon tubes",
        "trees": ["a dead sapling in a planter"],
        "out-door": "a steel door under a buzzing lamp",
        "light": [255, 100, 200],
        "prompts": {
            "floor": "Pixel art, gritty neon noir, wet concrete and pink light: cracked concrete."
        },
    }
    FakeImages.sent = []
    monkeypatch.setattr(session_area, "ImageClient", FakeImages)
    session = make_session(tmp_path, story, cast, responses=[json.dumps(design)])
    session.config.providers[0].api_key = "k"
    # The designer: the author's choice, else Opus 5.5 on NanoGPT.
    assert session.tileset_designer == "anthropic/claude-opus-5.5"
    session.config.tileset_model = "z-ai/glm-5.3-flash"
    assert session.tileset_designer == "z-ai/glm-5.3-flash"
    assert session.own_tile_set_offer()[2] is None
    assert session.start_tile_set_design(
        "gritty rain-slick cyberpunk alleys, neon and wet concrete"
    )
    session._design_thread.join(5)
    state, words, error = session.tile_set_design
    assert state == "done" and error is None and words is not None
    assert words["prompt:floor"] == design["prompts"]["floor"]
    assert "prompt:cap" not in words  # the template's, shown as it will be sent
    assert session.map_art_copy().tile_sets == {}  # nothing drawn yet
    sent = next(e for e in read_api_log(story.id, tmp_path) if e["kind"] == "map_tileset_request")
    assert sent["payload"]["model"] == "z-ai/glm-5.3-flash"
    assert "cyberpunk alleys, neon" in json.dumps(sent["payload"])
    count, price = session.tile_set_price(words)
    assert count == len(tile_jobs(words)) and price is None  # no listing: no guess
    # The author edits a prompt and names the set, then it is drawn as approved.
    words["prompt:floor"] = (
        "Cracked concrete, wet. Style: gritty neon noir, wet concrete and pink light."
    )
    assert session.start_own_tile_set(words, "Neon alleys")
    session._own_set_thread.join(10)
    art = session.map_art_copy()
    assert art.tile_set == "own:neon-alleys" and list(art.tile_sets) == ["neon-alleys"]
    own = art.tile_sets["neon-alleys"]
    assert own.title == "Neon alleys" and own.made_at
    assert set(own.assets) == {role for role, _p, _s in tile_jobs(words)}
    assert all(f.startswith("images/map/tileset/") for f in own.assets.values())
    assert own.words["prompt:floor"] == words["prompt:floor"]
    assert FakeImages.sent[0]["prompt"] == words["prompt:floor"]  # exactly as approved
    assert len(FakeImages.sent) == count
    kinds = [e["kind"] for e in read_api_log(story.id, tmp_path)]
    assert "map_tileset_response" in kinds and kinds.count("map_picture_response") == count
    # In use, listed, its words the prompts' style; drawn for this story.
    assert session.tile_set_name() == "own:neon-alleys"
    assert ("own:neon-alleys", "Neon alleys") in session.tile_set_choices()
    assert session.tile_style() == "gritty neon noir, wet concrete and pink light"
    assets = {a.role: a for a in session.tile_set_assets()}
    assert assets["floor"].source == "drawn for this story"
    assert assets["floor"].words == "cracked concrete"
    assert session.tile_asset_bytes("floor") == session.pictures.read(own.assets["floor"])
    cards = {c.name: c for c in session.tile_set_cards()}
    assert cards["own:neon-alleys"].own and cards["own:neon-alleys"].in_use
    assert cards["doomsville"].title == "Snowbound farm" and not cards["doomsville"].in_use
    assert cards["own:neon-alleys"].read("floor") == session.pictures.read(own.assets["floor"])
    # A second set beside it, swapped to and from; removing one deletes its pictures.
    assert session.start_own_tile_set(words, "Neon alleys")
    session._own_set_thread.join(10)
    art = session.map_art_copy()
    assert set(art.tile_sets) == {"neon-alleys", "neon-alleys-2"}
    assert art.tile_set == "own:neon-alleys-2"
    session.set_tile_set("own:neon-alleys")
    assert session.tile_set_name() == "own:neon-alleys"
    floor = art.tile_sets["neon-alleys"].assets["floor"]
    assert session.remove_own_tile_set("own:neon-alleys")
    assert session.pictures.read(floor) is None
    assert list(session.map_art_copy().tile_sets) == ["neon-alleys-2"]
    assert session.map_art_copy().tile_set == ""  # the program chooses again
    shipped = {name for name, _title in session.tile_set_choices() if not name.startswith("own:")}
    assert session.tile_set_name() in shipped
    assert not session.remove_own_tile_set("doomsville")


def test_someone_the_scene_has_here_and_the_map_never_placed_stands_with_the_author(
    tmp_path, story, cast
):
    story.scene.present_character_ids = ["char-serrik", "char-maela"]
    session = make_session(tmp_path, story, cast)
    assert "Maela Orr" not in session.world().where
    state = session.area_state()
    me, maela = state.spots["Serrik Vaun"], state.spots["Maela Orr"]
    assert maela.area == me.area == "house"
    session.story.scene.present_character_ids = ["char-serrik"]
    assert "Maela Orr" not in session.area_state().spots  # gone from the scene: from the map


def test_several_of_one_thing_within_reach_are_named_once():
    world, art = world_and_art()
    house = build_area(world, art, "house")
    from sealedlore.engine.area import Placement

    stools = [
        Placement(
            id=f"s{i}", name="a wooden stool", x=5 + i, y=10, w=1, h=1, kind="furniture", verbs=()
        )
        for i in range(3)
    ]
    house.placements = [*house.placements, *stools]
    text = render_positions(house, world, {"Jane": Spot(area="house", x=5, y=11)}, "Jane")
    reach = next(line for line in text.splitlines() if line.startswith("Within reach"))
    assert reach.count("wooden stool") == 1 and "2 more near" in reach


# --- the pixel art's style, and a card's avatar ---------------------------------------


def test_every_pixel_art_prompt_takes_the_set_s_style_and_none_names_a_game():
    """The author (Oct 16 2026): the world should feel like the player's own,
    not one game's look; the style suits the world (gritty, bright, whimsical…)."""
    from sealedlore.engine.prompt_texts import TEXTS
    from sealedlore.engine.tile_draw import DEFAULT_STYLE, style_words
    from sealedlore.storage.tile_sets import shipped_sets

    games = ("Stardew", "Graveyard Keeper", "Balrum", "Dwarf Fortress", "Zelda", "Minecraft")
    for key, text in TEXTS.items():
        if key.startswith("map"):
            assert not any(game in text.default for game in games), key
    assert style_words({"style": " gritty  and dark "}) == "gritty and dark"
    assert style_words({}) == DEFAULT_STYLE
    words = {
        "world": "a drowned city",
        "style": "bleak and cold, muted greys",
        "floor": "wet planks",
        "cap": "slate",
        "face": "slate wall",
        "rug": "a reed mat",
        "decor": ["a lamp", "a net", "a jar"],
        "out-ground": "mud",
        "out-road": "planks",
        "out-water": "green water",
        "out-pave": "stone",
        "out-canopy": "reeds",
        "out-roof": "thatch",
        "out-wall": "brick",
        "trees": ["a willow"],
        "out-bush": "reeds",
        "out-rock": "a rock",
        "out-rubble": "bricks",
        "out-fence": "a rail",
        "out-door": "a door",
    }
    parsed = parse_tile_words(json.dumps(words))
    assert parsed["style"] == "bleak and cold, muted greys"
    jobs = tile_jobs(parsed)
    assert all("bleak and cold, muted greys" in prompt for _role, prompt, _size in jobs)
    people = [("Jane", "images/refs/jane.jpg", "tall"), ("Ben", None, "a farmer")]
    walkers = walker_jobs(people, lambda name: False, "a farm", style="whimsical storybook")
    assert all("whimsical storybook" in w.prompt for w in walkers)
    # Every shipped set says what suits its world.
    sets = shipped_sets()
    assert sets and all(info.words.get("style") for info, _folder in sets.values())


def test_a_card_s_avatar_is_drawn_from_the_portrait_in_the_set_s_style_and_a_choice_holds(
    tmp_path, story, cast, monkeypatch
):
    from sealedlore.engine import session_area
    from sealedlore.models.image import GeneratedImage, ImageRef
    from tests.test_world_pictures import FakeImages, png

    FakeImages.sent = []
    monkeypatch.setattr(session_area, "ImageClient", FakeImages)
    session = make_session(tmp_path, story, cast)
    session.config.providers[0].api_key = "k"
    info, _read = session.tile_set()
    assert session.tile_style() == info.words["style"]
    # No avatar yet: a stand-in walks for them.
    assert session.walker_asset("Serrik Vaun") == (None, "")
    # With a portrait, "Draw the avatar" with no words of the author's own is
    # the portrait's edit (the card's words), in the set's style.
    serrik = next(c for c in session.cast if c.name == "Serrik Vaun")
    session.pictures.write("images/refs/serrik.png", png(8, 8))
    serrik.reference_images.insert(
        0, ImageRef(id="p1", file="images/refs/serrik.png", caption="portrait")
    )
    prompt, _size, model, reference, drawn_from = session._asset_job("walker/Serrik Vaun", "")
    assert reference is not None and "image 1" in prompt and info.words["style"] in prompt
    assert drawn_from == "images/refs/serrik.png"
    assert model == session.config.map_sprite_model
    assert session.redraw_tile_asset("walker/Serrik Vaun", "")
    session._asset_thread.join(5)
    data, source = session.walker_asset("Serrik Vaun")
    assert data and source == "changed for this story"
    # The author's choice stands: the standing approval never draws another
    # over it, nor over "the set's own" ("").
    session.config.map_art = True
    session.config.map_pictures_mode = "auto"
    _furnish, jobs = session.tile_art_jobs()
    assert not any(j.subject == "walker" and j.name == "Serrik Vaun" for j in jobs)
    session.reset_tile_asset("walker/Serrik Vaun")
    assert session.walker_asset("Serrik Vaun") == (None, "")
    _furnish, jobs = session.tile_art_jobs()
    assert not any(j.subject == "walker" and j.name == "Serrik Vaun" for j in jobs)
    # A portrait drawn for the card and taken onto it is named, for the
    # window to offer an avatar from it.
    session.pictures.write("images/new.png", png(8, 8))
    drawn = GeneratedImage(
        id="drawn-1",
        anchor_node_id=None,
        file="images/new.png",
        prompt="Serrik",
        model="klein",
        size="1k",
        card_id=serrik.id,
        card_first=True,
    )
    session.pictures.add_images([drawn])
    assert session.take_card_pictures() == ["Serrik Vaun"]
    assert session.portraits_taken == [serrik.id]
    assert session.take_card_pictures() == [] and session.portraits_taken == []


# --- the clock, the second review ------------------------------------------------------


def test_the_plot_s_read_is_told_the_walk_already_on_the_clock():
    """A walk's minutes went into the author turn's `elapsed` and the read
    rewound them with the skip, so a successful read lost the walk."""
    from sealedlore.engine.chronicle_read import build_read_messages
    from sealedlore.models.plot import Chronicle
    from tests.test_chronicle import plot

    messages = build_read_messages(
        plot=plot(),
        chronicle=Chronicle(minutes=600),
        author_turn="I set off.",
        passage="He reached the gate.",
        walked=120,
    )
    text = messages[-1].text
    assert "walked or travelled for about 120 minutes" in text
    assert "THE CLOCK, before the author's turn: Day 1 · 10:00" in text
    plain = build_read_messages(
        plot=plot(), chronicle=Chronicle(minutes=600), author_turn="", passage="x"
    )
    assert "walked or travelled" not in plain[-1].text


def test_a_travel_told_to_the_storyteller_lands_the_character_though_nothing_is_drawn(
    tmp_path, story, cast
):
    """Travel… to a place with no plan: the map section used to say the
    character was still at the place they left, and the positions block
    (no area to stand in) dropped the journey and the choice. Now they stand
    on the open country at the place's door until its plan is drawn."""
    session = make_session(tmp_path, story, cast, ["He walked.", "{}"])
    session.config.scene_reads = "manual"
    text = session.travel("town")
    list(session.send(turn(text)))
    sent = session.provider.requests[0].messages[-1].text
    assert "Serrik Vaun is at: Hellsville" in sent
    assert "- Serrik Vaun: in open country, outside Hellsville." in sent
    assert "Since the last passage:" in sent and "to Hellsville" in sent
    assert "The author chose" in sent  # the travel's own line
    # Without a plot, the map read is asked for the passage's minutes and
    # told the walk is counted; its answer is capped at a week, not dropped.
    read = session.provider.requests[-1].messages[-1].text
    assert '"minutes": <a number>' in read and "already counted" in read
    assert "SPOTS NEAR" not in read  # nothing to stand by in open country: no spots


def test_without_a_plot_a_director_skip_moves_the_map_s_clock_and_a_long_read_is_capped(
    tmp_path, story, cast
):
    from sealedlore.models.node import DIRECTOR_SPEAKER_ID

    read = json.dumps({"minutes": 30 * 24 * 60, "minutes_quote": "A month went by"})
    session = make_session(tmp_path, story, cast, ["Weeks passed.", "{}", "A month went by.", read])
    session.config.scene_reads = "manual"
    skip = TurnRequest(
        speaker_id=DIRECTOR_SPEAKER_ID,
        user_text="Three weeks pass. Serrik mends the roof.",
        controlled_character_id="char-serrik",
    )
    list(session.send(skip))
    assert session.map_clock()[0] == 22  # day 1 + 21
    list(session.send(turn("I wait.")))
    assert session.map_clock()[0] == 22 + 7  # a month, capped at a week


def test_take_back_keeps_the_turn_s_walking_and_a_moved_leaf_drops_a_walk(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["One.", "{}", "Two.", "{}"])
    session.config.scene_reads = "manual"
    text = session.travel("town")
    list(session.send(turn(text)))
    user = session.path()[-2]
    # Take back: the travel stands (applied once), ready for the turn again.
    session.restore_area(user)
    session.delete_from(user.id)
    live = session.story.area_live
    assert live is not None and live.leaf_id == session.story.active_leaf_id
    walked = live.state.walked
    assert walked > 0 and live.state.action.seconds == 0  # the way's length, once
    list(session.send(turn(text)))
    assert session.world().where["Serrik Vaun"] == "town"
    stamped = session.path()[-2].meta.area
    assert stamped.walked == walked and "to Hellsville" in stamped.walked_words


def test_a_walk_after_the_passage_is_dropped_by_a_retake_and_gone_for_good(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["A.", "{}", "B.", "{}"])
    session.config.scene_reads = "manual"
    list(session.send(turn("I look.")))
    house = session.area("house")
    door = next(e for e in house.exits if e[1] == 23)
    session.walked(session.walk_route(*door))
    assert session.story.area_live is not None
    old_leaf = session.story.active_leaf_id
    list(session.regenerate())
    assert session.story.active_leaf_id != old_leaf
    session.area_state()
    assert session.story.area_live is None  # not kept to come back on ‹


def test_at_moves_nobody_the_read_did_not_place(tmp_path, story, cast):
    """`at` alone used to move people the passage left where they were: the
    read echoed the turn's intent."""
    session = make_session(tmp_path, story, cast)
    token = session.mark_of_tile("farm", 30, 50)
    read = json.dumps({"at": {"Maela": token, "Serrik": "the cast-iron stove"}})
    session.provider.responses = ["Maela stayed by the stove.", read]
    session.config.scene_reads = "manual"
    session.story.scene.present_character_ids = ["char-serrik", "char-maela"]
    before = session.area_state().spots["Maela Orr"]
    list(session.send(turn(f"I wave Maela over to {token}.")))
    world = session.world()
    assert world.where.get("Maela Orr") in (None, "house")  # not sent to the farm
    after = session.path()[-1].meta.area.spots["Maela Orr"]
    assert (after.area, after.x, after.y) == (before.area, before.x, before.y)


def test_the_map_names_what_it_gave_up_drawing_and_can_be_asked_again(tmp_path, story, cast):
    """A job refused MAX_FAILURES times is never asked for again (live, a
    rift's sprite); the Map page names it and Try again clears the count."""
    from sealedlore.engine.world_art import MAX_FAILURES

    session = make_session(tmp_path, story, cast, [])
    world = session.world()
    farm = next(p for p in world.places.values() if p.name == "The farm")
    art = session.world_art
    art.failures[f"sprite:{farm.id}"] = MAX_FAILURES
    art.failures[f"layout:{farm.id}"] = MAX_FAILURES
    art.failures["terrain:window:0:0:800"] = MAX_FAILURES
    art.failures["sprite:nobody"] = 1  # once: still to be tried
    assert session.map_art_failed() == ["The farm", "the plan of The farm", "a stretch of land"]
    session.retry_map_art()
    assert session.world_art.failures == {"sprite:nobody": 1}
    assert session.map_art_failed() == [] and not session.retry_map_art()


# --- what became of the things (the author's ruling: in code) --------------------

LANTERN = {"kind": "object", "name": "a brass lantern", "x": 28, "y": 5}
BREAD = {"kind": "object", "name": "a loaf of bread", "x": 30, "y": 5}


def things_session(tmp_path, story, cast, responses=()):
    """The house with a lantern and a loaf beside the stove, the lantern on
    the map's own list too, Serrik standing among them, Maela beside him."""
    from sealedlore.models.area import LiveArea

    session = make_session(
        tmp_path,
        story,
        cast,
        responses,
        house_objects=[LANTERN, BREAD],
        things=[WorldThing(id="lantern", name="a brass lantern", at="house")],
    )
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area="house", x=26, y=5)
    state.spots["Maela Orr"] = Spot(area="house", x=26, y=7)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)
    return session


def _thing(session, part: str):
    return next(p for p in session.area("house").placements if part in p.name)


def _told(session) -> str:
    """The tail of the latest request to the storyteller (the map read's
    requests come after it)."""
    heading = "Where everyone stands"
    return next(
        r.messages[-1].text
        for r in reversed(session.provider.requests)
        if heading in r.messages[-1].text
    )


def test_a_thing_taken_is_off_the_floor_with_its_taker_and_the_map_knows(tmp_path, story, cast):
    session = things_session(tmp_path, story, cast, ["He pockets it.", "{}", "Nothing.", "{}"])
    lantern = _thing(session, "lantern")
    text = session.map_action("take", lantern.name, lantern.id)
    assert text == "I take the brass lantern."
    list(session.send(turn(text)))
    user = session.path()[-2]
    record = user.meta.area.things[lantern.id]
    assert record.state == "taken" and record.by == "Serrik Vaun"
    # The floor: the lantern gone, its tile plain floor to walk on.
    house = session.area("house")
    assert not any("lantern" in p.name for p in house.placements)
    assert house.walkable(28, 5)
    assert session.carried_things() == [(lantern.id, "brass lantern")]
    # The storyteller: told who carries it, and the map's own thing is his.
    sent = _told(session)
    assert "Serrik Vaun carries: brass lantern." in sent
    assert "- a brass lantern: carried by Serrik Vaun" in sent
    assert "the brass lantern (at hand" not in sent
    world = session.world()
    assert world.things["lantern"].carried_by == "Serrik Vaun"
    assert world.things["lantern"].at is None
    # The passage's own snapshot carries the record on.
    assert session.path()[-1].meta.area.things[lantern.id].state == "taken"
    # Still carried on the next turn, which does nothing to it.
    list(session.send(turn("I look around.")))
    assert "Serrik Vaun carries: brass lantern." in _told(session)


def test_a_thing_destroyed_stays_broken_and_one_eaten_is_gone(tmp_path, story, cast):
    replies = ["Crash.", "{}", "Crumbs.", "{}", "Quiet.", "{}"]
    session = things_session(tmp_path, story, cast, replies)
    stove = _thing(session, "stove")
    list(session.send(turn(session.map_action("destroy", stove.name, stove.id))))
    house = session.area("house")
    broken = next(p for p in house.placements if "stove" in p.name)
    assert broken.broken and "destroy" not in broken.verbs and "use" in broken.verbs
    assert not house.walkable(stove.x, stove.y)  # still standing there
    assert "the cast-iron stove (broken) (" in _told(session)
    bread = _thing(session, "bread")
    list(session.send(turn(session.map_action("eat", bread.name, bread.id))))
    house = session.area("house")
    assert not any("bread" in p.name for p in house.placements) and house.walkable(30, 5)
    assert session.carried_things() == []
    # A broken thing can't be destroyed again: the record stands as it is.
    assert session.area("house").placement_at(stove.x, stove.y).broken
    list(session.send(turn("I sit down.")))
    sent = _told(session)
    assert "(broken)" in sent and "bread" not in sent


def test_a_rewritten_turn_and_a_failed_roll_change_nothing(tmp_path, story, cast):
    from dataclasses import replace

    from sealedlore.models.node import Roll

    session = things_session(tmp_path, story, cast, ["One.", "{}", "Two.", "{}", "Three.", "{}"])
    lantern = _thing(session, "lantern")
    session.map_action("take", lantern.name, lantern.id)
    list(session.send(turn("I leave it where it is.")))  # the menu's words are gone
    assert session.path()[-2].meta.area.things == {}
    assert any("lantern" in p.name for p in session.area("house").placements)
    # A failed roll: the attempt is the storyteller's to tell; nothing changes hands.
    text = session.map_action("take", lantern.name, lantern.id)
    failed = replace(
        turn(text),
        agency_mode="dice",
        roll=Roll(die=100, value=99, band="failure", target=50),
    )
    list(session.send(failed))
    assert session.path()[-2].meta.area.things == {}
    assert session.carried_things() == []
    sent = _told(session)
    assert "The author chose Take on the map" in sent and "carries:" not in sent
    # A success at a cost is still a success.
    text = session.map_action("take", lantern.name, lantern.id)
    costly = replace(
        turn(text),
        agency_mode="dice",
        roll=Roll(die=100, value=45, band="success_at_a_cost", target=50),
    )
    list(session.send(costly))
    assert session.carried_things() == [(lantern.id, "brass lantern")]


def test_a_carried_thing_is_dropped_at_the_feet_or_given_to_someone_in_reach(tmp_path, story, cast):
    replies = ["A.", "{}", "B.", "{}", "C.", "{}", "D.", "{}"]
    session = things_session(tmp_path, story, cast, replies)
    lantern = _thing(session, "lantern")
    list(session.send(turn(session.map_action("take", lantern.name, lantern.id))))
    assert session.people_in_reach() == ["Maela Orr"]
    text = session.map_action("drop", "brass lantern", lantern.id)
    assert text == "I put the brass lantern down."
    list(session.send(turn(text)))
    record = session.path()[-2].meta.area.things[lantern.id]
    me = session.area_state().spots["Serrik Vaun"]
    assert record.state == "dropped" and record.spot is not None
    assert record.spot.area == "house"
    assert abs(record.spot.x - me.x) + abs(record.spot.y - me.y) <= 2
    house = session.area("house")
    dropped = next(p for p in house.placements if "lantern" in p.name)
    assert (dropped.x, dropped.y) == (record.spot.x, record.spot.y) and "take" in dropped.verbs
    assert session.carried_things() == []
    assert "The author chose Drop on the map" in _told(session)
    assert session.world().things["lantern"].at == "house"
    # Taken again, then given to Maela: hers now, told so.
    list(session.send(turn(session.map_action("take", dropped.name, dropped.id))))
    text = session.map_action("give", "brass lantern", lantern.id, to="Maela Orr")
    assert text == "I give the brass lantern to Maela Orr."
    list(session.send(turn(text)))
    assert session.carried_things() == [] and session.carried_things("Maela Orr") == [
        (lantern.id, "brass lantern")
    ]
    sent = _told(session)
    assert "hands the brass lantern to Maela Orr" in sent
    assert "Maela Orr carries: brass lantern." in sent
    assert session.world().things["lantern"].carried_by == "Maela Orr"
    assert not any("lantern" in p.name for p in session.area("house").placements)


def test_a_take_survives_take_back_and_a_retake_as_the_spots_do(tmp_path, story, cast):
    session = things_session(tmp_path, story, cast, ["One.", "{}", "Two.", "{}", "Three.", "{}"])
    session.config.scene_reads = "manual"
    lantern = _thing(session, "lantern")
    text = session.map_action("take", lantern.name, lantern.id)
    list(session.send(turn(text)))
    # Another take of the passage: the turn's own record, told again.
    list(session.regenerate())
    assert "Serrik Vaun carries: brass lantern." in _told(session)
    assert session.carried_things() == [(lantern.id, "brass lantern")]
    # Take back: the turn's own record goes with it (the lantern is on the
    # floor again), and comes back when the turn is sent again with the
    # menu's words; a turn rewritten without them takes nothing.
    user = session.path()[-2]
    session.restore_area(user)
    session.delete_from(user.id)
    assert lantern.id not in session.story.area_live.state.things
    assert session.carried_things() == []
    assert session.story.area_live.state.action is not None  # the choice still pending
    list(session.send(turn("I look at the lantern and leave it.")))
    assert session.carried_things() == []
    user = session.path()[-2]
    session.restore_area(user)
    session.delete_from(user.id)
    text = session.map_action("take", lantern.name, lantern.id)
    list(session.send(turn(text)))
    assert session.path()[-2].meta.area.things[lantern.id].by == "Serrik Vaun"
    assert session.carried_things() == [(lantern.id, "brass lantern")]


def test_the_time_of_day_has_its_bands_and_never_the_day():
    """The storyteller is told the time of day in words, never the day
    (the author's rule): one phrase from the clock's minute."""
    from sealedlore.engine.area import now_words, time_of_day

    assert time_of_day(0) == "the small hours"
    assert time_of_day(4 * 60 + 59) == "the small hours"
    assert time_of_day(5 * 60) == "early morning"
    assert time_of_day(8 * 60) == "mid-morning"
    assert time_of_day(11 * 60) == "midday"
    assert time_of_day(13 * 60) == "afternoon"
    assert time_of_day(16 * 60) == "late afternoon"
    assert time_of_day(18 * 60) == "dusk"
    assert time_of_day(19 * 60 + 30) == "evening"
    assert time_of_day(22 * 60) == "night"
    assert time_of_day(23 * 60 + 59) == "night"
    assert time_of_day(3 * 24 * 60 + 12 * 60) == "midday"  # the day is never counted
    assert now_words(12 * 60, "snowing hard") == "It is midday. The weather: snowing hard."
    assert now_words(18 * 60) == "It is dusk."
    assert now_words(None, " rain. ") == "The weather: rain."
    assert now_words(None, "") == ""


def test_the_positions_carry_the_time_of_day_and_the_weather_under_the_heading():
    world, art = world_and_art()
    house = build_area(world, art, "house")
    stove = next(p for p in house.placements if "stove" in p.name)
    spots = {"Jane": Spot(area="house", x=stove.x, y=stove.y + 1)}
    text = render_positions(
        house, world, spots, "Jane", now="It is dusk. The weather: snowing hard."
    )
    lines = text.splitlines()
    assert lines[0].startswith("Where everyone stands")
    assert lines[1] == "It is dusk. The weather: snowing hard."
    assert "Day" not in text and "day" not in text


def test_the_storyteller_is_told_the_time_of_day_and_the_weather_the_story_set(
    tmp_path, story, cast
):
    read = json.dumps({"starts_at": "06:10", "starts_at_quote": "Snow fell on the yard."})
    session = make_session(tmp_path, story, cast, ["Snow fell on the yard.", read, "Later.", "{}"])
    session.config.scene_reads = "manual"
    session.story.world_base.weather = "snowing hard"
    list(session.send(turn("I look up.")))
    sent = session.provider.requests[0].messages[-1].text
    heading = "Where everyone stands (the map is exact: keep to it):"
    assert heading in sent
    after = sent.split(heading, 1)[1].splitlines()
    # Without a plot nothing says the time before the opening: its read
    # starts the clock (it was "the small hours" for every opening).
    assert after[1] == "The weather: snowing hard."
    assert "Day 1" not in sent and "day 1" not in sent
    list(session.send(turn("I wait.")))
    sent = session.provider.requests[-2].messages[-1].text
    after = sent.split(heading, 1)[1].splitlines()
    assert after[1].startswith("It is ") and "small hours" not in after[1]
    assert after[1].endswith("The weather: snowing hard.")


def session_log(session) -> list[dict]:
    from sealedlore.storage.repository import read_api_log

    return read_api_log(session.story.id, session.root)


def test_without_a_plot_the_clock_starts_at_the_time_the_opening_is_set_at(tmp_path, story, cast):
    """It started at midnight whatever the opening said: a derelict ship's
    opening was told and painted as the small hours (Oct 17 2026)."""
    read = json.dumps({"starts_at": "19:30", "starts_at_quote": "The sun was setting."})
    session = make_session(tmp_path, story, cast, ["The sun was setting.", read, "Later.", "{}"])
    session.config.scene_reads = "manual"
    list(session.send(turn("I look out.")))
    assert session.map_clock() == (1, 19 * 60 + 30)
    first = [e for e in session_log(session) if e["kind"] == "world_request"]
    assert "starts_at" in json.dumps(first[0]["payload"])
    list(session.send(turn("I wait.")))
    second = [e for e in session_log(session) if e["kind"] == "world_request"]
    assert "starts_at" not in json.dumps(second[-1]["payload"])  # the opening's alone
    assert session.map_clock() == (1, 19 * 60 + 30)


def test_an_opening_whose_read_gives_no_time_starts_the_clock_in_the_morning(tmp_path, story, cast):
    from sealedlore.engine.session_area import DEFAULT_START_MINUTE, opening_minute

    session = make_session(tmp_path, story, cast, ["Somewhere.", "{}"])
    session.config.scene_reads = "manual"
    list(session.send(turn("I look out.")))
    assert session.map_clock() == (1, DEFAULT_START_MINUTE)
    assert opening_minute("7.05") == 7 * 60 + 5
    assert opening_minute("25:00") == opening_minute("dusk") == DEFAULT_START_MINUTE


def test_every_character_s_avatar_is_drawn_from_the_portrait_they_have_now(tmp_path, story, cast):
    """The author, Oct 17 2026: every character has an avatar or gets one.
    One the map drew from their words before a portrait came is drawn again
    from the portrait; one drawn from a portrait they no longer lead with,
    likewise. A sample's (drawn before this was kept) and the author's own
    choice stand."""
    from sealedlore.models.image import ImageRef
    from sealedlore.models.world import MapPicture

    session = make_session(tmp_path, story, cast)
    session.config.providers[0].api_key = "k"
    session.config.map_art = True
    assert session.config.map_pictures_mode == "auto" and session.map_pictures_on

    def walker_job(name: str):
        _furnish, jobs = session.tile_art_jobs()
        return next((j for j in jobs if j.subject == "walker" and j.name == name), None)

    def drawn(name: str, from_picture: str | None) -> None:
        art = session.world_art
        art.pictures.append(
            MapPicture(
                subject="walker",
                subject_id=name,
                name=name,
                file="images/map/w.png",
                from_picture=from_picture,
            )
        )

    job = walker_job("Serrik Vaun")
    assert job is not None and job.reference is None and job.from_picture == ""
    # While a portrait is being drawn for them, their avatar waits for it
    # rather than being drawn from their words and again once it lands.
    session.portraits_coming = frozenset({"char-serrik"})
    assert walker_job("Serrik Vaun") is None
    session.portraits_coming = frozenset()
    drawn("Serrik Vaun", "")  # from their words: no portrait came
    assert walker_job("Serrik Vaun") is None
    serrik = next(c for c in session.cast if c.name == "Serrik Vaun")
    serrik.reference_images.insert(0, ImageRef(id="p1", file="images/refs/p1.png"))
    job = walker_job("Serrik Vaun")
    assert job is not None and job.reference == job.from_picture == "images/refs/p1.png"
    drawn("Serrik Vaun", "images/refs/p1.png")
    assert walker_job("Serrik Vaun") is None
    serrik.reference_images.insert(0, ImageRef(id="p2", file="images/refs/p2.png"))
    assert walker_job("Serrik Vaun").from_picture == "images/refs/p2.png"
    # A sample's avatar (None: what it was drawn from isn't known) stands: a
    # new portrait is asked about instead.
    drawn("Serrik Vaun", None)
    assert walker_job("Serrik Vaun") is None and not session.avatar_due("Serrik Vaun")
    # One the map drew from their words is drawn again from the portrait.
    drawn("Serrik Vaun", "")
    assert session.avatar_due("Serrik Vaun")
    # The author's own choice stands.
    session.world_art.tile_overrides["walker/Serrik Vaun"] = "images/map/mine.png"
    assert walker_job("Serrik Vaun") is None and not session.avatar_due("Serrik Vaun")


def test_avatars_asked_for_while_one_is_drawing_join_its_run(tmp_path, story, cast, monkeypatch):
    """Live, portraits landed one after another and every avatar offered
    after the first was refused ("Another asset is being drawn now")."""
    import threading

    from sealedlore.engine import session_area
    from tests.test_world_pictures import FakeImages

    release = threading.Event()

    class SlowImages(FakeImages):
        def generate(self, payload):
            release.wait(10)
            return super().generate(payload)

    FakeImages.sent = []
    monkeypatch.setattr(session_area, "ImageClient", SlowImages)
    session = make_session(tmp_path, story, cast)
    session.config.providers[0].api_key = "k"
    assert session.redraw_tile_assets([("walker/Serrik Vaun", "")])
    assert session.tile_asset_offer("walker/Maela Orr", "")[2] is None
    assert session.redraw_tile_assets([("walker/Maela Orr", ""), ("walker/Serrik Vaun", "x")])
    assert session.redraw_tile_assets([("walker/Maela Orr", "")])  # already queued: once
    release.set()
    session._asset_thread.join(10)
    assert len(FakeImages.sent) == 3
    assert session.walker_asset("Maela Orr")[1] == "changed for this story"
    # Once the run is over, the next ask starts one of its own.
    assert session.redraw_tile_assets([("walker/Maela Orr", "y")])
    session._asset_thread.join(10)
    assert len(FakeImages.sent) == 4


def test_a_run_asked_for_while_one_draws_is_wanted_again_once_it_is_done(tmp_path, story, cast):
    """Live, portraits landed while the avatars were being drawn from the
    cards' words; the redraw was refused (a run was under way) and nothing
    drew them again. The window starts the run once the map is idle."""
    import threading

    session = make_session(tmp_path, story, cast)
    gate = threading.Event()
    busy = threading.Thread(target=gate.wait, daemon=True)
    busy.start()
    session._tile_thread = busy
    assert not session.map_art_wanted_again
    assert not session.start_tile_art() and session.map_art_wanted_again
    gate.set()
    busy.join(5)
    session.start_tile_art()  # nothing to draw (Draw the map is off): asked, and answered
    assert not session.map_art_wanted_again


def test_a_new_world_s_tile_set_is_chosen_by_all_its_words(tmp_path, story, cast):
    """Live, a snowbound fur-trading post's first lines named none of the
    sets' words and the wasteland's set, first by name, drew it."""
    session = make_session(tmp_path, story, cast)
    session.world_art.tile_set = ""
    session.story.world_bible = (
        "A thinly colonised northern territory, late in the season, where the trade in "
        "pelts holds the river posts together. "
        + "Licensed posts buy furs. " * 20
        + "In winter the factor keeps to his cabin."
    )
    assert session.tile_set_name() == "doomsville"
