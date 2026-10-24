"""Map-first play (engine/area.py, engine/furnish.py, engine/tile_sets.py,
engine/tile_draw.py, engine/session_area.py; SealedLoreDocs design/map-first.md):
the law layer, its rules, walking, standing, what the storyteller is told,
and the turn's walking carried into the story and the plot's clock."""

from __future__ import annotations

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
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
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
    assert item_slugs("A wooden chest") == ["a-wooden-chest", "wooden-chest", "the-wooden-chest"]
    assert item_slugs("armchair") == ["armchair", "an-armchair", "the-armchair"]
    assert own_key("Rain-slick alleys", []) == "rain-slick-alleys"
    assert own_key("Rain-slick alleys", ["rain-slick-alleys"]) == "rain-slick-alleys-2"
    assert own_key("", ["own"]) == "own-2"


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
    # The far side is offered too, from anywhere in the area (the author,
    # Oct 23 2026): the walk across the farm to its south edge counted in.
    south = session.edge_choices("south")
    assert south[0][1] == "open country" and south[0][2] > choices[0][2]
    tile, steps = session.edge_tile("south")
    assert tile.y == session.area("farm").height - 1 and steps[0] == (30, 0)
    assert steps[-1] == (tile.x, tile.y)
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


def test_a_landing_past_an_edge_steps_on_into_the_far_area_not_back():
    """The review (Oct 20 2026): `_landing` stepped "in" the wrong way (tile
    y grows south), so a line of trees just past an edge read as barred
    though the ground a tile beyond it was open."""
    from sealedlore.engine.area import Area
    from sealedlore.engine.session_area import AreaRuntime

    def area(rows: list[str]) -> Area:
        return Area(
            place_id="w",
            name="open country",
            indoor=False,
            look="grass",
            rows=rows,
            tile_m=2.5,
            objects={},
            labels={},
            rect=(0.0, 0.0, 22.5, 22.5),
        )

    # Leaving north: the far area's row at the spot is all trees; north of
    # it (smaller y) open ground, south of it (toward the area left) water.
    far = area(["........."] * 3 + ["TTTTTTTTT"] + ["~~~~~~~~~"] * 5)
    assert AreaRuntime._landing(far, Spot(area="w", x=4, y=3), "north") == (4, 2)
    far = area(["~~~~~~~~~"] * 3 + ["TTTTTTTTT"] + ["........."] * 5)
    assert AreaRuntime._landing(far, Spot(area="w", x=4, y=3), "south") == (4, 4)
    assert AreaRuntime._landing(area(["~~~T....."] * 9), Spot(area="w", x=3, y=4), "east") == (4, 4)
    assert AreaRuntime._landing(area([".....T~~~"] * 9), Spot(area="w", x=5, y=4), "west") == (4, 4)
    # Two lines deep is as far as it looks: a river is never crossed.
    far = area(["........."] * 3 + ["~~~~~~~~~"] * 3 + ["........."] * 3)
    assert AreaRuntime._landing(far, Spot(area="w", x=4, y=5), "north") is None
    # Tiles to avoid are never landed on, though walkable.
    far = area(["........."] * 9)
    assert AreaRuntime._landing(
        far, Spot(area="w", x=4, y=8), "north", avoid={(x, 8) for x in range(9)}
    ) == (4, 7)


def test_walking_on_into_open_country_never_lands_in_the_place_s_own_yard(tmp_path, story, cast):
    """The review (Oct 20 2026): on the window of open country a place with
    a plan is boxed as a fenced yard rounded out to whole 40 m tiles, and the
    first tile past the plan's edge lay inside that box (The Pilgrims' Rest's
    own gate or pavement), where `door_here` led straight back in."""
    session = make_session(tmp_path, story, cast)
    # The farm's 400 m plan off the land's 40 m grid, so its box on the
    # window runs a tile past the plan's edge (Doomsville's farm happens to
    # fill whole tiles, which is why it landed clear).
    farm = next(p for p in session.story.world_base.places if p.id == "farm")
    farm.x, farm.y = 10.0, 10.0
    _stand(session, "farm", 30, 0)
    state = session.cross_edge("north")
    me = state.spots["Serrik Vaun"]
    there = session.area(me.area)
    assert there.window and "farm" in there.buildings
    x0, y0, x1, y1 = there.buildings["farm"]
    assert not (x0 <= me.x <= x1 and y0 <= me.y <= y1)
    assert session.door_here() is None  # not on the farm's own gate


def test_a_correction_forgetting_the_area_cache_mid_build_breaks_nothing(
    tmp_path, story, cast, monkeypatch
):
    """The review (Oct 20 2026): a correction on the worker sets the area
    cache to None while the GUI thread may be building an area; the build
    read the attribute twice and failed on the second."""
    from sealedlore.engine import session_area

    session = make_session(tmp_path, story, cast)
    real = session_area.build_area

    def forgetful(*args, **kwargs):
        session._area_cache = None  # `_done`, meanwhile
        return real(*args, **kwargs)

    monkeypatch.setattr(session_area, "build_area", forgetful)
    assert session.area("house") is not None
    assert session.area("house") is not None


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


# --- the clock, the second review ------------------------------------------------------


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


def test_a_naming_puts_the_name_where_the_unknown_stood_with_what_they_carried():
    """Pure: the fold (`applied`). A cast member an earlier read placed
    elsewhere, named for the unknown here, is here."""
    from sealedlore.engine.world import world_at
    from sealedlore.models.node import Node, NodeMeta

    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=300.0)
    barn = WorldPlace(id="barn", name="The barn", kind="building", x=0.0, y=400.0, size=20.0)
    first = WorldDelta(
        places=[farm, barn],
        added=["farm", "barn"],
        where={"John": "farm", "Elena Ruiz": "barn", "Unknown 1": "farm"},
        outside={"Elena Ruiz": True},
        unknowns={"Unknown 1": "a girl in a red coat"},
        things=[WorldThing(id="t1", name="a brass key", carried_by="Unknown 1")],
    )
    second = WorldDelta(named={"Unknown 1": "Elena Ruiz"})
    nodes = [
        Node(kind="assistant", speaker_id="n", content="x", meta=NodeMeta(world=d))
        for d in (first, second)
    ]
    world = world_at(nodes)
    assert world.where == {"John": "farm", "Elena Ruiz": "farm"}
    assert world.outside == []  # the unknown wasn't outside anything
    assert world.unknowns == {} and world.things["t1"].carried_by == "Elena Ruiz"


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


def session_log(session) -> list[dict]:
    from sealedlore.storage.repository import read_api_log

    return read_api_log(session.story.id, session.root)


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


def test_a_road_through_a_town_wall_is_a_gate_and_a_building_s_wall_stays_shut():
    """Live (Oct 17 2026): Hellsville's wall closed its road at both gates
    (roads go on open ground only), and nobody could walk into the town."""
    from sealedlore.engine.area import _roads_on_open_ground

    rows = ["." * 40 for _ in range(12)]
    rows[5] = "#" * 40  # a town wall, east to west
    rows[8] = "." * 10 + "#" * 6 + "." * 24  # a building's wall, short
    across = {"kind": "road", "points": [[30, 0], [30, 11]], "width": 2}
    along = {"kind": "road", "points": [[0, 4], [39, 4]], "width": 2}
    through_building = {"kind": "road", "points": [[12, 7], [12, 11]], "width": 1}
    out = _roads_on_open_ground(rows, [across, along, through_building])
    assert "+" in out[5][28:33] and out[5].count("+") <= 4  # a gate where it crosses
    assert out[5][:25] == "#" * 25  # the road along the wall leaves it whole
    assert out[8][10:16] == "######"  # never through a building
