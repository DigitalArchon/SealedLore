"""Map-first play anywhere on a limitless map (engine/area.py `build_window`,
engine/world_art.py `window_rect` / `plan_art`, engine/session_area.py).

The author (Oct 16 2026): "Why can't it go wider than 2.5 km? The world
should be limitless, short of the limits of the player's hardware and funds
for API calls." So wherever the character is there is ground to stand on: a
plan where a place has one, else a window of the land under them, drawn a
patch at a time as they go.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sealedlore.engine.area import (
    area_of,
    build_area,
    build_window,
    front_of,
    path,
    point_tile,
    render_positions,
    tile_point,
)
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.world import resolve_reply
from sealedlore.engine.world_art import (
    PLAN_MAX_SIZE,
    TERRAIN_TILES,
    TILES,
    WINDOW_PATCHES,
    WINDOW_SIDE,
    adopt,
    finish,
    in_open_country,
    plan_art,
    rasterise,
    window_rect,
)
from sealedlore.engine.world_layout import size_of
from sealedlore.models.area import Spot
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.world import (
    PlaceLayout,
    TerrainPatch,
    WorldArt,
    WorldDelta,
    WorldMap,
    WorldPlace,
)
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from sealedlore.storage.world_art import save_world_art

# The land the character stands on: a 1.6 km patch of sand at the grid's
# own cell (0-1600 east, 0-1600 north), a rock outcrop and a pool in it.
DUNES_SPEC = {
    "ground": "sand",
    "features": [
        {"kind": "area", "fill": "rock", "x": 30, "y": 5, "w": 6, "h": 4},
        {"kind": "area", "fill": "water", "x": 5, "y": 30, "w": 4, "h": 3},
    ],
}
HERE = "window:0:1600:1600"


def open_world() -> tuple[WorldMap, WorldArt]:
    erg = WorldPlace(id="erg", name="The Great Erg", kind="region", x=800.0, y=800.0, size=50_000.0)
    wreck = WorldPlace(
        id="wreck", name="The wreck", kind="site", inside="erg", x=400.0, y=1200.0, size=60.0
    )
    oasis = WorldPlace(
        id="oasis", name="The oasis", kind="site", inside="erg", x=3000.0, y=800.0, size=200.0
    )
    city = WorldPlace(
        id="city", name="Qasr", kind="settlement", inside="erg", x=20_000.0, y=0.0, size=4_000.0
    )
    world = WorldMap(
        places={p.id: p for p in (erg, wreck, oasis, city)},
        seen=["erg", "wreck", "oasis", "city"],
        start="erg",
    )
    art = WorldArt()
    rows, _objects, _labels = rasterise(DUNES_SPEC, TERRAIN_TILES, TERRAIN_TILES)
    art.terrain.append(
        TerrainPatch(
            id="dunes", place_id="erg", x=800.0, y=800.0, side=1600.0, rows=rows, ground="sand"
        )
    )
    spec = {
        "ground": "sand",
        "features": [{"kind": "object", "name": "a rusted hull", "x": 20, "y": 12}],
    }
    rows, objects, labels = rasterise(spec, 40, 24)
    art.layouts["wreck"] = {
        "": PlaceLayout(
            place_id="wreck", spec=spec, rows=rows, objects=objects, labels=labels, made_at="1"
        )
    }
    return world, art


def open_session(tmp_path: Path, story, cast, responses=()) -> StorySession:
    world, art = open_world()
    story.defaults.world_map = True
    story.world_base = WorldDelta(places=list(world.places.values()), seen=list(world.seen))
    story.world_start = "erg"
    story.held_character_id = "char-serrik"
    story.scene.present_character_ids = ["char-serrik"]
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


# --- the window ------------------------------------------------------------------------


def test_in_open_country_the_land_under_the_character_is_the_area():
    world, art = open_world()
    # A region has no plan: the window of the land at the character's point,
    # named by its rectangle, so it needs no state and stands whether or not
    # its land is drawn.
    assert area_of(world, art, "erg", (800.0, 800.0)) == HERE
    assert area_of(world, art, None, (800.0, 800.0)) == HERE  # no place at all
    assert area_of(world, art, "wreck") == "wreck"  # a place with a plan keeps it
    window = build_window(world, art, HERE)
    assert window.window and window.id == HERE and not window.indoor
    # The patch's own tile: 1.6 km over 40 columns, a long stride, and honest.
    assert window.tile_m == pytest.approx(40.0) and (window.width, window.height) == (40, 40)
    assert window.look == "sand" and window.patch_id == "dunes"
    # The place holding it, for "in open country, in The Great Erg".
    assert window.place_id == "erg" and window.within == "The Great Erg"
    # The land's rock and water are where its rows have them, in the plan's legend.
    assert window.rows[6][31] == "^" and window.rows[31][6] == "~"
    assert all(ch in TILES for row in window.rows for ch in row)
    # The wreck lies in the patch: a box with a door that leads into its plan,
    # fenced round pavement (a site's grounds, not one roofed building:
    # the renderer roofs walled rings only).
    assert "wreck" in window.buildings and [e.place_id for e in window.entrances] == ["wreck"]
    assert any("gave The wreck a door" in r for r in window.repairs)
    x0, y0, x1, y1 = window.buildings["wreck"]
    assert x1 - x0 + 1 >= 4 and window.rows[y0][x0] == "%"
    assert window.rows[y0 + 1][x0 + 1] == "_" and window.walkable(x0 + 1, y0 + 1)
    assert build_window(world, art, "wreck") is None  # not a window's id


def test_a_window_s_tile_is_a_point_on_the_map_and_back():
    world, art = open_world()
    window = build_window(world, art, HERE)
    assert tile_point(world, window, 0, 0) == (20.0, 1580.0)
    for tile in ((0, 0), (17, 33), (39, 39)):
        point = tile_point(world, window, *tile)
        assert point_tile(world, window, *point) == tile
    assert point_tile(world, window, 1700.0, 800.0) is None  # past its east edge


def test_without_land_a_plain_window_stands_in_and_the_patch_is_wanted():
    world, art = open_world()
    # East of the dunes nothing is drawn: the grid cell there, plain sand
    # (the land drawn nearest), so there is ground to walk on meanwhile.
    assert window_rect(art, 2000.0, 800.0) == (1600.0, 1600.0, 1600.0)
    plain = build_window(world, art, "window:1600:1600:1600")
    assert plain.patch_id == "" and plain.look == "sand"
    assert set("".join(plain.rows)) <= set("s%+_")  # sand, and the oasis fenced at its edge
    assert "oasis" in plain.buildings and plain.entrances == []  # no plan of its own yet
    assert build_window(WorldMap(), WorldArt(), "window:0:0:1600").look == "dirt"
    # In open country the land under the character is asked for (the cell
    # under them first), then what its edges lead to, a few at a time.
    assert in_open_country(world, art, "erg") and not in_open_country(world, art, "wreck")
    jobs = plan_art(world, art, here="erg", setting="x", point=(2000.0, 800.0))
    windows = [j for j in jobs if j.key.startswith("terrain:window:")]
    assert windows[0].key == "terrain:window:1600:1600:1600"
    assert len(windows) <= WINDOW_PATCHES and windows[0].patch == (2400.0, 800.0, WINDOW_SIDE)
    assert "the open country" in windows[0].messages[-1].text
    assert "in The Great Erg" in windows[0].messages[-1].text
    assert "The oasis" in windows[0].messages[-1].text  # kept clear of water and rock
    # On the dunes, the cell under them is drawn already: only the neighbours.
    jobs = plan_art(world, art, here="erg", setting="x", point=(800.0, 800.0))
    keys = [j.key for j in jobs if j.key.startswith("terrain:window:")]
    assert "terrain:window:0:1600:1600" not in keys and "terrain:window:1600:1600:1600" in keys
    # At a place whose plan is still to be drawn, no windows: its plan is on the way.
    town = WorldPlace(id="town", name="Qalat", kind="settlement", x=9000.0, y=9000.0, size=1500.0)
    world.places["town"] = town
    assert not in_open_country(world, art, "town")
    assert not any(
        j.key.startswith("terrain:window:")
        for j in plan_art(world, art, here="town", setting="x", point=(9000.0, 9000.0))
    )
    # The patch drawn for the cell fills the same window in: the id stands.
    rock = {"kind": "area", "fill": "rock", "x": 2, "y": 2, "w": 3, "h": 3}
    reply = json.dumps({"ground": "sand", "features": [rock]})
    patch = finish(windows[0], reply, model="m", made_at="t")
    assert patch.place_id == "erg" and (patch.x, patch.y, patch.side) == (2400.0, 800.0, 1600.0)
    adopt(art, patch)
    drawn = build_window(world, art, "window:1600:1600:1600")
    assert drawn.patch_id == patch.id and drawn.rows[2][2] == "^"


def test_a_window_s_cell_is_finer_where_the_grid_cell_s_middle_is_under_land_already():
    world, art = open_world()
    # A place's own land lies anywhere on the map, never on the grid: a
    # patch over the middle of the cell east of the dunes. The point past
    # it is under no land, and the cell there would never be drawn (land on
    # land is dropped), so a finer cell is the window: 800 m, then 400 m.
    art.terrain.append(
        TerrainPatch(place_id="oasis", x=2300.0, y=700.0, side=800.0, rows=["s" * 40] * 40)
    )
    assert window_rect(art, 3100.0, 1500.0) == (2400.0, 1600.0, 800.0)
    assert build_window(world, art, "window:2400:1600:800").tile_m == pytest.approx(20.0)
    assert window_rect(art, 2000.0, 200.0) == (2000.0, 400.0, 400.0)
    assert build_window(world, art, "window:2000:400:400").tile_m == pytest.approx(10.0)
    # On the patch itself, the patch: its own rows at its own tile.
    assert window_rect(art, 2300.0, 700.0) == (1900.0, 1100.0, 800.0)


# --- the session ----------------------------------------------------------------------


def test_the_character_stands_at_their_point_and_walks_into_a_place_by_its_door(
    tmp_path, story, cast
):
    session = open_session(tmp_path, story, cast)
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    assert me.area == HERE and (me.x, me.y) == (20, 20)  # the erg's spot, mid-window
    window = session.area(HERE)
    assert session.here_place() == "erg"
    door = front_of(window, "wreck")
    steps = session.walk_route(*door)
    assert steps and steps[-1] == door
    state = session.walked(steps)
    assert state.walked > 60  # 40 m strides: a short walk on the clock
    entrance = window.entrances[0]
    session.walked(session.walk_route(entrance.x, entrance.y))
    assert session.door_here() == ("wreck", "The wreck")
    state = session.go_through()
    assert state.spots["Serrik Vaun"].area == "wreck"
    assert "from open country to The wreck" in state.walked_words


def test_walking_off_a_window_s_edge_leads_into_the_next(tmp_path, story, cast):
    from sealedlore.models.area import LiveArea

    session = open_session(tmp_path, story, cast)

    def stand(x: int, y: int) -> None:
        state = session.area_state()
        state.spots["Serrik Vaun"] = Spot(area=HERE, x=x, y=y)
        session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)

    stand(39, 20)
    assert session.edge_here() == "east"
    choices = session.edge_choices("east")
    assert choices[0][:2] == ("area", "open country (The Great Erg)")
    assert [c[1] for c in choices[1:]] == ["The oasis", "Qasr"]  # the places that way
    # No land east yet: a plain window, walked into all the same.
    state = session.cross_edge("east")
    me = state.spots["Serrik Vaun"]
    assert me.area == "window:1600:1600:1600" and me.x == 0 and 0 < state.walked < 60
    assert session.area(me.area).patch_id == ""
    # With land drawn there, the same step lands on it.
    rows, _o, _l = rasterise({"ground": "sand", "features": []}, TERRAIN_TILES, TERRAIN_TILES)
    session.world_art.terrain.append(
        TerrainPatch(id="east", place_id="erg", x=2400.0, y=800.0, side=1600.0, rows=rows)
    )
    session._area_cache = None
    stand(39, 20)
    state = session.cross_edge("east")
    assert session.area(state.spots["Serrik Vaun"].area).patch_id == "east"


def test_the_storyteller_is_told_the_country_and_a_mark_in_it_resolves(tmp_path, story, cast):
    session = open_session(tmp_path, story, cast, ["Sand. Nothing but sand.", "{}"])
    token = session.mark_of_tile(HERE, 10, 10)
    assert token == "⌖(420, 1180)"
    list(session.send(turn(f"I walk toward {token}.")))
    sent = session.provider.requests[0].messages[-1].text
    assert "- Serrik Vaun: in open country, in The Great Erg." in sent
    user = session.path()[-2]
    (mark,) = user.meta.area.marks
    # A region is country, not a place to be at (engine/marks.py): the mark
    # lies in no place, on the window's tile, and its words say the country.
    assert mark.place_id is None and mark.spot == Spot(area=HERE, x=10, y=10)
    assert (
        mark.words.startswith("open country, ") and "of The wreck, in The Great Erg" in mark.words
    )
    assert "of Serrik Vaun" in mark.words
    # Where the author stands on the map: their tile's point, not the erg's.
    assert session.held_point() == pytest.approx((820.0, 780.0))
    # Someone the scene has here stands beside them on the window.
    session.story.scene.present_character_ids = ["char-serrik", "char-maela"]
    spots = session.area_state().spots
    assert spots["Maela Orr"].area == HERE
    assert abs(spots["Maela Orr"].x - 20) + abs(spots["Maela Orr"].y - 20) <= 6


def test_a_read_that_names_the_character_elsewhere_moves_them_and_one_that_doesn_t_leaves_them(
    tmp_path, story, cast
):
    from sealedlore.models.area import LiveArea

    read = json.dumps({"where": [{"who": "Serrik", "place": "The wreck", "quote": "the wreck"}]})
    session = open_session(tmp_path, story, cast, ["He reached the wreck.", read, "Wind.", "{}"])
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area=HERE, x=5, y=5)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)
    list(session.send(turn("I walk to the wreck.")))
    assert session.world().where["Serrik Vaun"] == "wreck"
    assert session.path()[-1].meta.area.spots["Serrik Vaun"].area == "wreck"
    # Back out into the country: the read says nothing of them, so they stay
    # where they walked to (the map's `where` has no word for nowhere).
    wreck = session.area("wreck")
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area=HERE, x=30, y=30)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)
    list(session.send(turn("I walk on.")))
    me = session.path()[-1].meta.area.spots["Serrik Vaun"]
    assert me.area == HERE and (me.x, me.y) == (30, 30)
    assert wreck.place_id == "wreck"


def test_positions_on_a_window_name_the_country_and_the_people_on_it():
    world, art = open_world()
    window = build_window(world, art, HERE)
    spots = {"Jane": Spot(area=HERE, x=20, y=20), "Ben": Spot(area=HERE, x=22, y=20)}
    text = render_positions(window, world, spots, "Jane")
    assert "- Jane: in open country, in The Great Erg." in text
    assert "- Ben: 80 m east of Jane; in sight" in text  # 40 m a tile
    assert render_positions(window, world, {"Jane": Spot(area="erg", x=1, y=1)}, "Jane") == ""


# --- a place too wide for a local area still gets a plan -------------------------------


def test_a_city_wider_than_a_local_area_gets_a_plan_at_a_coarser_grain():
    world, art = open_world()
    world.visited = {"Jane": ["city"]}
    world.where = {"Jane": "city"}
    jobs = {j.key: j for j in plan_art(world, WorldArt(), here="city", setting="x")}
    assert "layout:city" in jobs and jobs["layout:city"].extra["grid"] == (80, 48)
    assert not any(k.startswith("terrain:window:") for k in jobs)  # its plan is on the way
    rows, _o, _l = rasterise({"ground": "dirt", "features": []}, 80, 48)
    art.layouts["city"] = {
        "": PlaceLayout(place_id="city", spec={"ground": "dirt", "features": []}, rows=rows)
    }
    assert area_of(world, art, "city") == "city"
    city = build_area(world, art, "city")
    # 4 km over 80 columns is 50 m a plan tile; the walking grid is scaled
    # by 2 (160 tiles at most): 25 m a tile, 160 x 96.
    assert city.tile_m == pytest.approx(25.0) and (city.width, city.height) == (160, 96)
    # Someone in a building in it with no inside drawn stands on the city's plan.
    inn = WorldPlace(id="inn", name="The inn", kind="building", inside="city", x=20_100.0, y=0.0)
    world.places["inn"] = inn
    assert area_of(world, art, "inn") == "city"
    # Wider than the grid can carry: open country, a window.
    waste = WorldPlace(id="waste", name="The waste", kind="site", x=-30_000.0, y=0.0, size=9_000.0)
    world.places["waste"] = waste
    world.visited = {"Jane": ["waste"]}
    assert size_of(waste) > PLAN_MAX_SIZE
    assert "layout:waste" not in [j.key for j in plan_art(world, art, here="waste", setting="x")]
    assert area_of(world, art, "waste").startswith("window:")


# --- the view ---------------------------------------------------------------------------


def test_a_window_is_drawn_as_any_outdoor_plan_is():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from sealedlore.engine.tile_sets import TileSetInfo
    from sealedlore.gui.area_render import OUTDOOR_PX, TileSource, render_area

    QApplication.instance() or QApplication([])
    world, art = open_world()
    window = build_window(world, art, HERE)
    image = render_area(window, TileSource(TileSetInfo(name="plain"), lambda role: None))
    assert not image.isNull() and image.width() == window.width * OUTDOOR_PX


def test_stood_on_the_land_until_their_place_had_a_plan_they_go_into_it_once_it_has(
    tmp_path, story, cast
):
    """Live (a new world, Oct 17 2026): the opening was read before any plan
    was drawn, so everyone stood on the land; when the plans came, the
    others went into the cabin and the author's character stayed out in the
    trading post's yard, while the story had her in the cabin."""
    read = json.dumps({"where": [{"who": "Serrik", "place": "The wreck", "quote": "the wreck"}]})
    session = open_session(tmp_path, story, cast, ["He is in the wreck.", read, "Wind.", "{}"])
    layout = session.world_art.layouts.pop("wreck")
    session._area_cache = None
    list(session.send(turn("I shelter in the wreck.")))
    me = session.path()[-1].meta.area.spots["Serrik Vaun"]
    assert me.area.startswith("window:") and me.for_place == "wreck"
    session.world_art.layouts["wreck"] = layout  # its plan is drawn
    session._area_cache = None
    assert session.area_state().spots["Serrik Vaun"].area == "wreck"
    list(session.send(turn("I look around.")))
    assert session.path()[-1].meta.area.spots["Serrik Vaun"].area == "wreck"


def test_a_companion_at_the_same_place_stands_on_the_author_s_window_beside_them(
    tmp_path, story, cast
):
    """The replay (Oct 18 2026): Tobias, walking the river road with Wren,
    was stood on the next window over, by his own last spot: the road's spot
    lay on the line between two windows, and he wasn't beside her."""
    session = open_session(tmp_path, story, cast)
    world, art = session.world(), session.map_art_copy()
    world.where["Maela"] = "erg"
    state = session.area_state()
    state.spots["Maela"] = Spot(area="window:1600:1600:1600", x=0, y=20)
    settled = session.settle(state, world, art, walked=False)
    me, her = settled.spots["Serrik Vaun"], settled.spots["Maela"]
    assert her.area == me.area == HERE
    assert abs(her.x - me.x) <= 3 and abs(her.y - me.y) <= 3


COTTAGE_ROWS = [
    "########",
    "#______#",
    "#______#",
    "#______#",
    "#______#",
    "###+####",
]


@pytest.mark.parametrize("inside", ["erg", None])
def test_a_house_on_no_plan_is_left_by_its_door_onto_the_land_round_it(
    tmp_path, story, cast, inside
):
    """AfterDark (Oct 18 2026): John walked to the farmhouse door and nothing
    happened. The farmhouse stood alone on the land, so its door led nowhere;
    in a region with no plan it led to the window at the region's middle, not
    the house's. Out of the door, a turn has him at the house, outside it."""
    session = open_session(tmp_path, story, cast, ["Cold air.", "{}"])
    cottage = WorldPlace(
        id="cottage", name="The cottage", kind="building", inside=inside,
        x=2400.0, y=1200.0, size=15.0,
    )  # fmt: skip
    session.story.world_base.places.append(cottage)
    session.story.world_base.seen.append("cottage")
    session.story.world_start = "cottage"
    session.world_art.layouts["cottage"] = {
        "": PlaceLayout(place_id="cottage", spec={}, rows=COTTAGE_ROWS, made_at="1")
    }
    session._area_cache = None
    me = session.area_state().spots["Serrik Vaun"]
    house = session.area("cottage")
    assert me.area == "cottage" and house.indoor and house.exits
    door = house.exits[0]
    session.walked(session.walk_route(*door))
    leads, name = session.door_here()
    assert leads.startswith("window:") and leads != HERE  # the house's own ground
    assert name == "The Great Erg"  # the region the land lies in, by where it is
    state = session.go_through()
    out = state.spots["Serrik Vaun"]
    land = session.area(out.area)
    assert out.area == leads and "cottage" in land.buildings
    list(session.send(turn("I step out and look at the sky.")))
    world = session.world()
    assert world.where["Serrik Vaun"] == "cottage" and "Serrik Vaun" in world.outside
    assert session.area_state().spots["Serrik Vaun"].area == leads  # not pulled back in


def ways_in_and_out(session: StorySession, place_id: str) -> list[str]:
    """What stops someone getting into or out of a place with an inside: []
    when every way out leads onto ground, the ground round it has a door in,
    going in lands on floor, and a way out can be walked to from there. The
    author (Oct 18 2026): no building you can't enter and leave."""
    world, art = session.world(), session.map_art_copy()
    held = session.held_name()
    inside = session.area(place_id, world, art)
    name = world.places[place_id].name

    def put(spot: Spot) -> None:
        state = session.area_state()
        state.spots[held] = spot
        session._set_live(state)

    if inside is None or not inside.indoor:
        return [f"{name}: no inside"]
    problems = [] if inside.exits else [f"{name}: no way out"]
    for ex in inside.exits:
        put(Spot(area=inside.id, x=ex[0], y=ex[1]))
        if session.door_here() is None:
            problems.append(f"{name}: the way out at {ex} leads nowhere")
            continue
        out = session.go_through().spots[held]
        there = session.area(out.area, world, art)
        if there is None or not there.walkable(out.x, out.y):
            problems.append(f"{name}: out at {ex} lands on no ground")
    around = session.area(inside.parent, world, art) if inside.parent else None
    doors = [e for e in around.entrances if e.place_id == place_id] if around else []
    if not doors:
        problems.append(f"{name}: no door in from outside")
    for door in doors:
        put(Spot(area=around.id, x=door.x, y=door.y))
        state = session.go_through()
        spot = state.spots[held] if state else None
        if spot is None or spot.area != inside.id or not inside.walkable(spot.x, spot.y):
            problems.append(f"{name}: in by {(door.x, door.y)} lands on no floor")
        elif not any(path(inside, (spot.x, spot.y), ex) for ex in inside.exits):
            problems.append(f"{name}: no way out can be walked to from the door in")
    return problems


def test_a_house_in_a_camp_with_no_plan_has_its_door_on_the_land(tmp_path, story, cast):
    """Older Doomsville stories (Oct 18 2026): the farm was never drawn, so
    the land showed it as a fenced yard and left the farmhouse out (it would
    be on the farm's own plan): its way out worked, and nothing led in."""
    session = open_session(tmp_path, story, cast)
    camp = WorldPlace(
        id="camp", name="The camp", kind="site", inside="erg", x=2400.0, y=400.0, size=200.0
    )
    hut = WorldPlace(
        id="hut", name="The hut", kind="building", inside="camp", x=2400.0, y=400.0, size=12.0
    )
    session.story.world_base.places += [camp, hut]
    session.story.world_base.seen += ["camp", "hut"]
    session.story.world_start = "hut"
    session.world_art.layouts["hut"] = {
        "": PlaceLayout(place_id="hut", spec={}, rows=COTTAGE_ROWS, made_at="1")
    }
    session._area_cache = None
    land = session.area(session.area("hut").parent)
    assert {"camp", "hut"} <= set(land.buildings)  # the hut stands in the camp's yard
    assert ways_in_and_out(session, "hut") == []


def test_a_vehicle_drawn_sealed_gets_a_door_but_not_a_deck_in_space():
    rows = ["########", "#______#", "#______#", "########"]
    for kind, look, doors in (("vehicle", "", 1), ("building", "", 1), ("vehicle", "space", 0)):
        place = WorldPlace(id="v", name="The wagon", kind=kind, x=0.0, y=0.0, size=10.0)
        world = WorldMap(places={"v": place}, seen=["v"])
        art = WorldArt()
        spec = {"ground": look} if look else {}
        art.layouts["v"] = {"": PlaceLayout(place_id="v", spec=spec, rows=rows, made_at="1")}
        assert len(build_area(world, art, "v").exits) == doors, (kind, look)


def _the_village(session, *, where: str) -> None:
    """A village on the erg (no plan of its own yet), an inn in it with its
    stable yard and front room, and someone at `where`, outside."""
    places = [
        WorldPlace(
            id="village", name="Carrow End", kind="settlement", inside="erg",
            x=2400.0, y=1200.0, size=600.0,
        ),
        WorldPlace(
            id="inn", name="The inn", kind="building", inside="village",
            x=2400.0, y=1200.0, size=15.0,
        ),
        WorldPlace(
            id="yard", name="the stable yard", kind="area", inside="inn",
            x=2410.0, y=1190.0, size=10.0,
        ),
        WorldPlace(
            id="front", name="the front room", kind="room", inside="inn",
            x=2400.0, y=1200.0, size=6.0,
        ),
    ]  # fmt: skip
    base = session.story.world_base
    base.places += places
    base.seen += [p.id for p in places]
    base.where["Serrik Vaun"] = where
    base.outside["Serrik Vaun"] = True
    session.world_art.layouts["inn"] = {
        "": PlaceLayout(place_id="inn", spec={}, rows=COTTAGE_ROWS, made_at="1")
    }
    session._area_cache = None


def test_walking_in_from_a_doorstep_keeps_them_inside_on_the_next_turn(tmp_path, story, cast):
    """The lab test (Oct 18 2026): Maren, read on her doorstep (outside her
    cottage), walked in by the door, and the next turn put her back outside:
    the doorstep belongs to the cottage, so standing in it read as still at
    its door."""
    session = open_session(tmp_path, story, cast, ["He comes in, dripping.", "{}"])
    cottage = WorldPlace(
        id="cottage", name="The cottage", kind="building", inside="erg",
        x=2400.0, y=1200.0, size=15.0,
    )  # fmt: skip
    step = WorldPlace(
        id="step", name="the doorstep", kind="area", inside="cottage",
        x=2400.0, y=1190.0, size=3.0,
    )  # fmt: skip
    base = session.story.world_base
    base.places += [cottage, step]
    base.seen += ["cottage", "step"]
    base.where["Serrik Vaun"] = "step"
    base.outside["Serrik Vaun"] = True
    session.world_art.layouts["cottage"] = {
        "": PlaceLayout(place_id="cottage", spec={}, rows=COTTAGE_ROWS, made_at="1")
    }
    session._area_cache = None
    me = session.area_state().spots["Serrik Vaun"]
    assert me.area.startswith("window:")  # on the doorstep, outside
    land = session.area(me.area)
    door = next(e for e in land.entrances if e.place_id == "cottage")
    session.walked(session.walk_route(door.x, door.y))
    session.go_through()
    assert session.area_state().spots["Serrik Vaun"].area == "cottage"
    list(session.send(turn("I let him in out of the rain.")))
    world = session.world()
    assert world.where["Serrik Vaun"] == "cottage" and "Serrik Vaun" not in world.outside
    assert session.area_state().spots["Serrik Vaun"].area == "cottage"


def test_someone_outside_on_the_land_goes_onto_the_village_s_plan_once_it_is_drawn(
    tmp_path, story, cast
):
    """The lab test (Oct 18 2026): Maren travelled to a village not drawn yet
    and was read at its inn's stable yard; stood on the open country, she
    stayed there once the village's plan came, the stable boy on it."""
    session = open_session(tmp_path, story, cast)
    _the_village(session, where="yard")
    state = session.area_state()
    assert state.spots["Serrik Vaun"].area.startswith("window:")
    assert state.spots["Serrik Vaun"].for_place == "yard"  # waiting for the plan round it
    session._set_live(state)
    rows, objects, labels = rasterise({"ground": "dirt", "features": []}, 40, 24)
    session.world_art.layouts["village"] = {
        "": PlaceLayout(place_id="village", spec={}, rows=rows, objects=objects, made_at="1")
    }
    session._area_cache = None
    me = session.area_state().spots["Serrik Vaun"]
    assert me.area == "village"  # on the village's plan, at the inn
    assert "inn" in session.area("village").buildings


def test_a_read_that_puts_someone_in_a_room_has_them_inside_unless_at_its_door(
    tmp_path, story, cast
):
    """The lab test (Oct 18 2026): Maren, outside at the stable yard, read in
    the inn's front room as the passage had her, was kept out in the street:
    from elsewhere, the read's placing needed the quote to say she went in."""
    session = open_session(tmp_path, story, cast)
    _the_village(session, where="yard")
    world = session.world()

    def outside_after(quote: str) -> bool:
        reply = {"where": [{"who": "Serrik Vaun", "place": "the front room", "quote": quote}]}
        delta, _refused = resolve_reply(world, reply, source=quote, people=[])
        return delta.outside["Serrik Vaun"]

    assert outside_after("The front room was low-ceilinged and almost empty.") is False
    assert outside_after("Serrik waits on the steps outside the front room.") is True
