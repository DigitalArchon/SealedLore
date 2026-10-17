"""The second review of map-first play (Oct 16 2026): the rules it left,
each held by a test. Engine only; the views' are in tests/test_area_view.py
and tests/test_world_view.py."""

from __future__ import annotations

import json
import threading
import time

import pytest

from sealedlore.engine.area import (
    anchor_of,
    build_area,
    is_deck,
    metres_per_tile,
    stand_near,
)
from sealedlore.engine.marks import marks_in, place_at
from sealedlore.engine.session_area import AreaRuntime, _asset_key
from sealedlore.engine.tile_draw import parse_tile_words, tile_jobs
from sealedlore.engine.tile_sets import TileSetInfo, choose_set, item_slugs, slug
from sealedlore.engine.world import applied, describe_delta, render_map_context, resolve_reply
from sealedlore.engine.world_art import plan_tile, rasterise
from sealedlore.engine.world_layout import MAX_DISTANCE, bearing, metres, place_new
from sealedlore.models.area import LiveArea, Spot
from sealedlore.models.world import (
    OwnTileSet,
    PlaceLayout,
    TerrainPatch,
    WorldArt,
    WorldDelta,
    WorldMap,
    WorldPlace,
    WorldThing,
)
from tests.test_area import FARM_SPEC, HOUSE_SPEC, make_session, turn, world_and_art

# --- the law layer ---------------------------------------------------------------------


def test_a_map_building_s_inside_is_not_the_yard_and_the_way_in_is_its_door():
    """Outdoors a map building's box was drawn as floor: anyone stood "at"
    it was stood under the roof, and a click past the door walked in."""
    world, art = world_and_art()
    farm = build_area(world, art, "farm")
    x0, y0, x1, y1 = farm.buildings["house"]
    inside = [(x, y) for y in range(y0 + 1, y1) for x in range(x0 + 1, x1)]
    assert not any(farm.walkable(x, y) for x, y in inside)
    way_in = AreaRuntime._way_in(farm)
    assert farm.walkable(*way_in) and not (x0 <= way_in[0] <= x1 and y0 <= way_in[1] <= y1)
    door = next(e for e in farm.entrances if e.place_id == "house")
    assert way_in == (door.x, door.y + 1)  # in front of its door


def test_a_door_opens_onto_open_ground_never_a_pond():
    world, art = world_and_art()
    spec = {
        **FARM_SPEC,
        "features": [
            *FARM_SPEC["features"],
            # A pond under the house's south side: the default door opened onto it.
            {"kind": "area", "fill": "water", "x": 0, "y": 11, "w": 40, "h": 4},
        ],
    }
    rows, objects, _labels = rasterise(spec, 40, 24)
    art.layouts["farm"] = {
        "": PlaceLayout(place_id="farm", spec=spec, rows=rows, objects=objects, made_at="1")
    }
    farm = build_area(world, art, "farm")
    door = next(e for e in farm.entrances if e.place_id == "house")
    front = anchor_of(farm, world, "house")
    assert front is not None and farm.walkable(*front)
    assert farm.at(*front) != "~" and (front[0], front[1]) != (door.x, door.y)


def test_nobody_keeps_standing_in_fire_after_a_plan_is_redrawn(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    art = session.world_art
    burnt = [list(r) for r in art.layouts["house"][""].rows]
    burnt[me.y][me.x] = "*"
    art.layouts["house"]["burnt"] = PlaceLayout(
        place_id="house", state="burnt", rows=["".join(r) for r in burnt], spec=HOUSE_SPEC
    )
    session._area_cache = None
    house = session.world().places["house"]
    session.story.world_base.places = [
        p.model_copy(update={"state": "burnt"}) if p.id == "house" else p
        for p in session.story.world_base.places
    ]
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)
    after = session.area_state().spots["Serrik Vaun"]
    area = session.area("house")
    assert house.id == "house" and area.walkable(after.x, after.y)
    assert session.walk_route(after.x + 1, after.y) or session.walk_route(after.x, after.y + 1)


def test_one_scale_for_the_grid_and_the_map():
    world, art = world_and_art()
    for place_id in ("house", "farm"):
        area = build_area(world, art, place_id)
        assert metres_per_tile(world, area) == pytest.approx(area.tile_m)
    house = build_area(world, art, "house")
    assert house.tile_m == pytest.approx(plan_tile(world.places["house"]))


def test_a_deck_is_known_by_the_land_it_stands_on():
    """The enclosed prompt asks for the hull as "floor": a station laid
    out so was built as an outdoor field."""
    world, art = world_and_art()
    station = WorldPlace(id="dock", name="The station", kind="site", x=0.0, y=20000.0, size=400.0)
    world.places["dock"] = station
    spec = {
        "ground": "floor",
        "features": [
            {"kind": "area", "fill": "dark", "x": 0, "y": 0, "w": 40, "h": 24},
            {
                "kind": "building",
                "x": 10,
                "y": 6,
                "w": 20,
                "h": 12,
                "doors": [{"side": "south", "at": 10}],
            },
        ],
    }
    rows, objects, _labels = rasterise(spec, 40, 24)
    art.layouts["dock"] = {"": PlaceLayout(place_id="dock", spec=spec, rows=rows, objects=objects)}
    assert not is_deck(world, art, "dock")
    art.terrain.append(
        TerrainPatch(place_id="dock", x=0.0, y=20000.0, side=1600.0, rows=["."], ground="space")
    )
    assert is_deck(world, art, "dock")
    assert build_area(world, art, "dock").indoor


def test_a_breach_in_a_ruin_s_wall_is_a_way_out_and_its_plan_is_not_re_walled():
    world, art = world_and_art()
    rows = [list(r) for r in art.layouts["house"][""].rows]
    for x in range(8, 18):
        rows[23][x] = ":"  # the south wall fell to rubble
    burnt = ["".join(r) for r in rows]
    art.layouts["house"]["blown up"] = PlaceLayout(
        place_id="house", state="blown up", rows=burnt, spec=HOUSE_SPEC
    )
    world.places["house"] = world.places["house"].model_copy(update={"state": "blown up"})
    house = build_area(world, art, "house")
    assert any(y == 23 and 8 <= x < 18 for x, y in house.exits)
    assert all(house.rows[23][x] == ":" for x in range(8, 18))  # not walled again


def test_a_sealed_room_gets_no_door_cut_for_it():
    world, art = world_and_art()
    spec = {"ground": "floor", "features": [{"kind": "building", "x": 0, "y": 0, "w": 40, "h": 24}]}
    rows, objects, _labels = rasterise(spec, 40, 24)
    for what in ("A study.", "A locked study, no way out."):
        world.places["house"] = world.places["house"].model_copy(update={"what": what})
        art.layouts["house"] = {"": PlaceLayout(place_id="house", spec=spec, rows=rows)}
        house = build_area(world, art, "house")
        if "locked" in what:
            assert house.exits == []
        else:
            assert house.exits  # a door is cut, as before


def test_a_walk_never_ends_on_another_person_s_tile(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    state = session.area_state()
    me, other = state.spots["Serrik Vaun"], state.spots["Maela Orr"]
    assert me.area == other.area == "house"
    route = session.walk_route(other.x, other.y)
    assert route and tuple(route[-1]) != (other.x, other.y)
    steps = [(me.x, me.y), (other.x, other.y)]
    if abs(me.x - other.x) + abs(me.y - other.y) == 1:
        assert session.walked(steps).spots["Serrik Vaun"] == me


def test_a_changed_building_shows_its_state_on_the_yard():
    """A barn blown up was still a whole roofed box with a door on the play view."""
    world, art = world_and_art()
    rows = [list(r) for r in art.layouts["house"][""].rows]
    for x in range(0, 40):
        rows[0][x] = ":"  # the whole north wall gone
    for y in range(2, 10):
        for x in range(22, 38):
            rows[y][x] = "*"
    gone = ["".join(r) for r in rows]
    art.layouts["house"]["blown up"] = PlaceLayout(
        place_id="house", state="blown up", rows=gone, spec=HOUSE_SPEC
    )
    world.places["house"] = world.places["house"].model_copy(update={"state": "blown up"})
    farm = build_area(world, art, "farm")
    x0, y0, x1, y1 = farm.buildings["house"]
    assert all(farm.rows[y0][x] == ":" for x in range(x0, x1 + 1))  # rubble, no roof ring
    assert any("*" in farm.rows[y][x0 + 1 : x1] for y in range(y0 + 1, y1))


def test_furnishing_is_asked_again_once_a_state_has_a_plan_of_its_own():
    from sealedlore.models.area import FurnishPlan

    art = WorldArt()
    art.furnishings["house"] = {"": FurnishPlan(place_id="house")}
    assert art.furnishing_for("house", "burnt") is art.furnishings["house"][""]
    art.layouts["house"] = {"burnt": PlaceLayout(place_id="house", state="burnt", rows=["#"])}
    assert art.furnishing_for("house", "burnt") is None  # its rooms are numbered afresh


# --- the map core ------------------------------------------------------------------------


def test_a_take_switched_brings_no_ghost_places_back():
    """A delta that merely changed a known place carried a whole copy; on the
    other take's line the copy came back as a second place."""
    world = WorldMap(places={"farm": WorldPlace(id="farm", name="The farm", x=0.0, y=0.0)})
    shed = WorldPlace(id="shed1", name="The shed", kind="building", inside="farm", x=30.0, y=0.0)
    made = WorldDelta(places=[shed], added=["shed1"])
    changed = WorldDelta(
        places=[shed.model_copy(update={"state": "burnt out"})], added=[], where={"Jane": "shed1"}
    )
    on_its_line = applied(applied(world, made), changed)
    assert on_its_line.places["shed1"].state == "burnt out"
    other_line = applied(world, changed)  # the take that never made the shed
    assert "shed1" not in other_line.places and "Jane" not in other_line.where
    legacy = applied(world, WorldDelta(places=[shed]))  # a delta from before: every place counts
    assert "shed1" in legacy.places


def test_an_imported_place_with_in_and_no_at_is_placed_by_its_parent():
    town = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=8000.0, size=1500.0)
    hall = WorldPlace(id="hall", name="The town hall", kind="building", inside="town")
    world = applied(WorldMap(), WorldDelta(places=[town, hall]), base=True)
    placed = world.places["hall"]
    assert placed.x is not None and abs(placed.x) < 750 and abs(placed.y - 8000.0) < 750


def test_a_read_s_reply_resolves_the_direction_s_changes():
    world, _art = world_and_art()
    world.places["house"] = world.places["house"].model_copy(update={"state": "burnt out"})
    delta, _refused = resolve_reply(
        world,
        {"places": [{"name": "The house", "state": "rebuilt", "quote": "The house stood rebuilt"}]},
        source="The house stood rebuilt.",
        people=[],
    )
    assert delta.places[0].state == ""  # as it was, not a state drawn as a change
    assert "The house now as it was" in describe_delta(world, delta)
    told = render_map_context(applied(world, delta), "Jane")
    assert "Now:" not in told
    world.where["Jane"] = "house"
    assert "Now: burnt out." in render_map_context(world, "Jane")


def test_the_newest_things_reach_the_storyteller_and_what_they_carry_first():
    world, _art = world_and_art()
    world.where["Jane"] = "farm"
    for i in range(10):
        world.things[f"t{i}"] = WorldThing(id=f"t{i}", name=f"thing {i}", at="farm")
    world.things["mine"] = WorldThing(id="mine", name="a lantern", carried_by="Jane")
    told = render_map_context(world, "Jane")
    lines = [line for line in told.splitlines() if line.startswith("- ")]
    assert lines[0].startswith("- a lantern: carried by Jane")
    assert "thing 9" in told and "thing 0" not in told


def test_up_and_down_are_not_bearings_and_light_years_are_capped():
    assert bearing("up") is None and bearing("downstairs") is None and bearing("north") == 0
    assert metres("five light-years") == MAX_DISTANCE and metres("3 km") == 3000.0


def test_a_crowded_parent_shrinks_a_child_rather_than_putting_it_outside():
    from sealedlore.engine.world_layout import _in_parent

    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=120.0)
    world = WorldMap(places={"farm": farm})
    sheds = [
        WorldPlace(id=f"s{i}", name=f"Shed {i}", kind="building", inside="farm", size=40.0)
        for i in range(6)
    ]
    place_new(world, sheds, [], None)
    for shed in sheds:
        world.places[shed.id] = shed
    assert all(_in_parent(farm, s.x, s.y, s.size) for s in sheds)


def test_a_mark_among_small_places_goes_to_the_one_whose_middle_is_nearest():
    world, art = world_and_art()
    for i, x in enumerate((100.0, 112.0)):
        world.places[f"shed{i}"] = WorldPlace(
            id=f"shed{i}", name=f"Shed {i}", kind="building", inside="farm", x=x, y=-100.0, size=6.0
        )
    assert place_at(world, art, 101.0, -100.0) == "shed0"
    assert place_at(world, art, 111.0, -100.0) == "shed1"


def test_a_thing_at_a_marked_spot_is_at_the_place_there(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    token = session.mark_of_tile("farm", 30, 50)
    read = json.dumps(
        {"things": [{"name": "the lantern", "at": token, "quote": "set the lantern down"}]}
    )
    session.provider.responses = ["She set the lantern down.", read]
    session.config.scene_reads = "manual"
    list(session.send(turn(f"I set the lantern down at {token}.")))
    lantern = next(t for t in session.world().things.values() if t.name == "the lantern")
    assert lantern.at == "farm"


# --- tile sets ---------------------------------------------------------------------------


def test_keywords_match_whole_words_and_names_in_other_scripts_still_slug():
    shipped = {
        "farm": TileSetInfo(name="farm", keywords=["farm", "inn"]),
        "station": TileSetInfo(name="station", keywords=["star", "ship"], deck=True),
    }
    assert choose_set("", "A modern inner-city story. It starts at dawn.", [], shipped) == "farm"
    assert choose_set("", "Two ships under the stars.", [], shipped) == "station"
    assert slug("温泉").startswith("x") and len(slug("温泉")) == 9
    assert item_slugs("温泉") == [slug("温泉")]


def test_a_story_s_own_set_needs_only_the_core_roles():
    words = {
        "world": "a city under the sea",
        "style": "cold and blue",
        "floor": "tiles",
        "cap": "steel",
        "face": "a bulkhead",
        "rug": "a mat",
        "out-ground": "sand",
        "out-road": "a walkway",
        "out-wall": "a dome wall",
    }
    parsed = parse_tile_words(json.dumps(words))
    roles = [role for role, _prompt, _size in tile_jobs(parsed)]
    assert "out-roof" not in roles and "floor" in roles and "rug" in roles
    with pytest.raises(ValueError):
        parse_tile_words(json.dumps({**words, "floor": ""}))


def test_back_to_the_set_s_own_loses_nothing_the_story_drew(tmp_path, story, cast, monkeypatch):
    from tests.test_world_pictures import png

    session = make_session(tmp_path, story, cast)
    own = png(8, 8)
    session.pictures.write("images/map/tileset/floor-own.png", own)
    art = session.world_art
    art.tile_set = "own:own"
    art.tile_sets["own"] = OwnTileSet(
        assets={"floor": "images/map/tileset/floor-own.png"}, words={"world": "x", "style": "neon"}
    )
    session.use_own_tile_asset("floor", png(16, 16))
    assert session.tile_asset_bytes("floor") != own
    session.reset_tile_asset("floor")
    assert session.tile_asset_bytes("floor") == own  # the own set's, not nothing
    _info, read = session.tile_set()
    assert read.from_story("floor")
    # An own set's words stay out of a shipped set's prompts.
    assert session.tile_style() == "neon"
    art.tile_set = "doomsville"
    assert session.tile_style() != "neon"


def test_the_standing_approval_and_the_author_s_draw_never_draw_one_figure_twice(
    tmp_path, story, cast
):
    session = make_session(tmp_path, story, cast)
    session.config.map_art = True
    session.config.map_pictures_mode = "auto"
    session.config.image_endpoint.base_url = "https://nano-gpt.com/api/v1"
    session.config.image_endpoint.api_key = "k"
    assert _asset_key("walker/Serrik Vaun") == "walker:Serrik Vaun"
    assert _asset_key("items/a-chest") == "item:a-chest"
    busy = threading.Thread(target=time.sleep, args=(0.4,), daemon=True)
    busy.start()
    session._asset_thread, session._asset_role = busy, "walker/Serrik Vaun"
    _furnish, jobs = session.tile_art_jobs()
    assert not any(j.key == "walker:Serrik Vaun" for j in jobs)
    busy.join()
    session._tile_thread = threading.Thread(target=time.sleep, args=(0.4,), daemon=True)
    session._tile_thread.start()
    session._tile_keys = {"walker:Serrik Vaun"}
    assert "standing approval" in session.tile_asset_offer("walker/Serrik Vaun", "")[2]
    session._tile_thread.join()


def test_stand_near_finds_a_tile_off_an_unwalkable_start():
    world, art = world_and_art()
    farm = build_area(world, art, "farm")
    x0, y0, x1, y1 = farm.buildings["house"]
    x, y = stand_near(farm, ((x0 + x1) // 2, (y0 + y1) // 2), ())
    assert farm.walkable(x, y)
    assert marks_in("⌖(1, 2)")[0].x == 1.0
    assert Spot(area="farm", x=x, y=y).area == "farm"


def test_a_burning_building_with_its_walls_whole_is_drawn_open_never_roofed():
    """Live (Oct 17 2026): the barn's burning plan kept every outer wall and
    had a few small fires; sampled a point a tile, the yard drew it whole
    and roofed."""
    from sealedlore.gui.map_tiles import building_boxes

    world, art = world_and_art()
    rows = [list(r) for r in art.layouts["house"][""].rows]
    for y, x in ((3, 3), (3, 4), (14, 30), (15, 31)):
        rows[y][x] = "*"  # two small fires, walls untouched
    art.layouts["house"]["burning fiercely"] = PlaceLayout(
        place_id="house", state="burning fiercely", rows=["".join(r) for r in rows], spec=HOUSE_SPEC
    )
    world.places["house"] = world.places["house"].model_copy(update={"state": "burning fiercely"})
    farm = build_area(world, art, "farm")
    x0, y0, x1, y1 = farm.buildings["house"]
    inside = [farm.rows[y][x] for y in range(y0 + 1, y1) for x in range(x0 + 1, x1)]
    assert "*" in inside and " " not in inside  # the fire shows; nothing left under a roof
    roofed = [b for b in building_boxes(farm.rows) if b[0] <= x0 <= b[1] and b[2] <= y0 <= b[3]]
    assert roofed == []
    # Unchanged, the same building is still roofed.
    world.places["house"] = world.places["house"].model_copy(update={"state": ""})
    whole = build_area(world, art, "farm")
    assert any(b[0] <= x0 <= b[1] and b[2] <= y0 <= b[3] for b in building_boxes(whole.rows))
