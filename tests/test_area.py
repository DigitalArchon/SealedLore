"""Map-first play (engine/area.py, engine/furnish.py, engine/tile_sets.py,
engine/tile_draw.py, engine/session_area.py; SealedLoreDocs design/map-first.md):
the law layer, its rules, walking, standing, what the storyteller is told,
and the turn's walking carried into the story and the plot's clock."""

from __future__ import annotations

import json
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
from sealedlore.engine.tile_draw import item_jobs, parse_tile_words, tile_jobs, walker_jobs
from sealedlore.engine.tile_sets import TileSetInfo, choose_set, item_slugs
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.world_art import rasterise
from sealedlore.models.area import FurnishItem, FurnishPlan, FurnishRoom, Spot
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.world import PlaceLayout, WorldArt, WorldDelta, WorldMap, WorldPlace
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


def world_and_art() -> tuple[WorldMap, WorldArt]:
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
    rows, objects, labels = rasterise(HOUSE_SPEC, 40, 24)
    art.layouts["house"] = {
        "": PlaceLayout(
            place_id="house",
            spec=HOUSE_SPEC,
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
    assert area_of(world, art, "town") is None  # no plan drawn yet


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
    assert choose_set("own", "", [], shipped) == "own"
    assert choose_set("", "Nothing in particular.", [], shipped) == "elf"  # the first, by name
    assert item_slugs("A wooden chest") == ["a-wooden-chest", "wooden-chest"]


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


# --- the session ---------------------------------------------------------------------


def make_session(tmp_path: Path, story, cast, responses=(), *, plot=None) -> StorySession:
    world, art = world_and_art()
    story.defaults.world_map = True
    story.world_base = WorldDelta(places=list(world.places.values()), seen=list(world.seen))
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
    assert 5000 < choices["Hellsville"] < 9000  # 8 km, winding, on foot
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
    assert "Serrik Vaun" not in stamped.spots  # Hellsville has no plan yet
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
    assert state.seconds == 3600
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
    assert [c[1] for c in choices] == ["Hellsville"]  # 8 km north
    assert session.edge_choices("south") == []
    assert session.cross_edge("north") is None  # nothing adjoins


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


def test_a_story_s_own_tile_set_and_its_changes_go_into_a_new_playthrough():
    from sealedlore.storage.world_art import carried_art

    art = WorldArt(
        tile_assets={"floor": "images/map/tileset/floor-1.png", "cap": "images/map/gone.png"},
        tile_overrides={"out-road": "images/map/tileset/road-1.png", "rug": ""},
    )
    files = {"images/map/tileset/floor-1.png": b"F", "images/map/tileset/road-1.png": b"R"}
    carried, out = carried_art(art, files.get)
    assert carried.tile_assets == {"floor": "images/map/tileset/floor-1.png"}  # only with a file
    assert carried.tile_overrides == {"out-road": "images/map/tileset/road-1.png", "rug": ""}
    assert out == files


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
