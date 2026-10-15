"""The map's art (engine/world_art.py, engine/session_world_art.py,
storage/world_art.py).

The model names shapes; the program cleans and draws them, keeps each piece
once made, and never redraws a place: the story has been there, so that is
what it looks like. A changed thing is drawn as an edit of its last sprite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.world import applied, place_delta
from sealedlore.engine.world_art import (
    LAYOUT_HEIGHT,
    LAYOUT_WIDTH,
    TERRAIN_TILES,
    WAY_PATCH_SIDE,
    ArtJob,
    adopt,
    clean_svg,
    finish,
    fixed_in_plan,
    local_area,
    plan_art,
    plan_rect,
    rasterise,
    with_fixed,
)
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.world import (
    PlaceLayout,
    WorldArt,
    WorldDelta,
    WorldLink,
    WorldMap,
    WorldPlace,
    WorldSprite,
    WorldThing,
)
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, read_api_log, save_story_bundle
from sealedlore.storage.world_art import load_world_art

TRUCK = (
    '```svg\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect x="8" y="30" width="30" height="20" fill="#b3384a"/>'
    '<circle cx="16" cy="48" r="7" fill="#1b1d2b"/></svg>\n```'
)

# --- SVG, cleaned ----------------------------------------------------------------


def test_a_clean_svg_keeps_only_plain_shapes():
    dirty = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" onload="alert(1)">'
        "<script>alert(1)</script>"
        '<g transform="translate(2,2)"><rect x="1" y="1" width="4" height="4" fill="#b3384a"'
        ' onclick="x()" style="fill:red"/></g>'
        '<image href="https://example.com/a.png" width="10" height="10"/>'
        '<text x="1" y="1">hi</text>'
        '<circle cx="5" cy="5" r="2" fill="url(#g)" stroke="#1b1d2b"/>'
        "<foreignObject><div>x</div></foreignObject>"
        "</svg>"
    )
    svg = clean_svg(dirty)
    assert svg is not None
    for gone in (
        "script",
        "onload",
        "onclick",
        "style",
        "image",
        "href",
        "text",
        "url(",
        "foreign",
    ):
        assert gone not in svg
    assert 'viewBox="0 0 64 64"' in svg
    assert '<rect x="1" y="1" width="4" height="4" fill="#b3384a"' in svg
    assert 'transform="translate(2,2)"' in svg
    assert '<circle cx="5" cy="5" r="2" stroke="#1b1d2b"' in svg


@pytest.mark.parametrize(
    "text",
    [
        "no svg here",
        '<svg xmlns="http://www.w3.org/2000/svg"><text>only words</text></svg>',
        '<svg><rect width="&a;" height="1"/></svg>',  # an entity: never declared, refused
        "<svg><rect width='1'",
    ],
)
def test_nothing_worth_keeping_is_none(text):
    assert clean_svg(text) is None


# --- layouts and land -------------------------------------------------------------


def test_a_building_is_drawn_with_closed_walls_and_its_door():
    rows, objects, labels = rasterise(
        {
            "ground": "snow",
            "features": [
                {
                    "kind": "building",
                    "name": "Farmhouse",
                    "x": 2,
                    "y": 2,
                    "w": 6,
                    "h": 4,
                    "doors": [{"side": "north", "at": 2}],
                },
                {"kind": "road", "points": [[4, 0], [4, 1]]},
                {"kind": "object", "name": "Old truck", "x": 10, "y": 3},
                {"kind": "area", "fill": "water", "x": 38, "y": 22, "w": 9, "h": 9},
            ],
        },
        LAYOUT_WIDTH,
        LAYOUT_HEIGHT,
    )
    assert len(rows) == LAYOUT_HEIGHT and {len(r) for r in rows} == {LAYOUT_WIDTH}
    assert rows[2][2:8] == "##+###"
    assert rows[5][2:8] == "######"
    assert rows[3][2] == "#" and rows[3][7] == "#" and rows[3][3:7] == "____"
    assert rows[0][4] == "=" and rows[1][4] == "="
    assert rows[3][10] == "1" and objects == {"1": "Old truck"}
    assert labels == {"Farmhouse": [5, 4]}
    assert rows[23][39] == "~"  # clipped at the edge, not an error


# --- plans on the map: the local area, and what the map fixes in a plan ------------


def doomsville() -> WorldMap:
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400.0)
    house = WorldPlace(
        id="house", name="The farmhouse", kind="building", inside="farm", x=0.0, y=50.0
    )
    barn = WorldPlace(id="barn", name="The barn", kind="building", inside="farm", x=120.0, y=-40.0)
    pond = WorldPlace(
        id="pond", name="The frozen pond", kind="area", inside="farm", x=-120.0, y=-90.0, size=60.0
    )
    town = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=8000.0, size=1500.0)
    hills = WorldPlace(id="hills", name="The hills", kind="area", x=4800.0, y=8600.0, size=3000.0)
    caves = WorldPlace(
        id="caves", name="The caves", kind="site", inside="hills", x=5200.0, y=9100.0, size=200.0
    )
    return WorldMap(
        places={p.id: p for p in (farm, house, barn, pond, town, hills, caves)},
        links=[WorldLink(a="farm", b="town", direction="north", distance="five miles")],
        seen=["farm", "town"],
        start="house",
    )


def test_the_local_map_is_the_outermost_place_up_to_a_few_km_wide():
    world = doomsville()
    assert local_area(world, "house").id == "farm"  # the farm, not its farmhouse
    assert local_area(world, "caves").id == "caves"  # the hills are country, not a place
    assert local_area(world, "town").id == "town"
    assert local_area(world, "nowhere") is None


def test_a_plan_lies_on_the_map_where_its_place_is():
    world = doomsville()
    assert plan_rect(world, world.places["farm"]) == (-200.0, 120.0, 400.0, 240.0)
    west, north, width, height = plan_rect(world, world.places["house"])
    assert (width, height) == (40.0, 24.0) and (west, north) == (-20.0, 62.0)


def test_the_map_s_buildings_are_fixed_in_a_plan_where_the_map_has_them():
    world = doomsville()
    fixed = {f["name"]: f for f in fixed_in_plan(world, world.places["farm"])}
    house, barn, pond = fixed["The farmhouse"], fixed["The barn"], fixed["The frozen pond"]
    # Ten-metre tiles: the farmhouse 50 m north of the middle is 5 rows up.
    assert (house["kind"], house["x"], house["y"], house["w"], house["h"]) == (
        "building",
        18,
        6,
        4,
        3,
    )
    assert barn["x"] == 30 and barn["y"] == 15 and pond["kind"] == "place" and pond["w"] == 6
    spec = {
        "ground": "snow",
        "features": [
            {
                "kind": "building",
                "name": "the farmhouse",
                "x": 2,
                "y": 2,
                "w": 8,
                "h": 6,
                "doors": [{"side": "north", "at": 7}],
            },
            {"kind": "building", "name": "A shed", "x": 30, "y": 2, "w": 4, "h": 3},
            {"kind": "area", "fill": "ice", "x": 8, "y": 15, "w": 6, "h": 4},
        ],
    }
    kept = with_fixed(spec, list(fixed.values()))
    names = [(f.get("name"), f["x"], f["y"]) for f in kept["features"] if f["kind"] == "building"]
    # The model's farmhouse is dropped and drawn where the map has it, with its door kept
    # on a side it has; the shed it invented stays; the barn gets a door of its own.
    assert ("the farmhouse", 2, 2) not in names and ("A shed", 30, 2) in names
    house_drawn = next(f for f in kept["features"] if f.get("name") == "The farmhouse")
    assert (house_drawn["x"], house_drawn["y"]) == (18, 6)
    assert house_drawn["doors"] == [{"side": "north", "at": 2}]  # clamped into its 4 tiles
    barn_drawn = next(f for f in kept["features"] if f.get("name") == "The barn")
    assert barn_drawn["doors"] == [{"side": "south", "at": 2}]
    job = next(
        j for j in plan_art(world, WorldArt(), here="house", setting="x") if j.key == "layout:farm"
    )
    ask = job.messages[1].text
    assert "where they stay" in ask and "The farmhouse (building): x 18-21, y 6-8" in ask
    plan = finish(job, json.dumps(spec), model="m", made_at="t")
    assert plan.rows[6][18:22] == "##+#" and plan.labels["The farmhouse"] == [20, 7]
    assert plan.labels["The frozen pond"] == [8, 21]  # named where the map has it, undrawn


def test_the_local_area_and_the_building_s_inside_are_planned_at_the_start():
    world = doomsville()
    keys = [j.key for j in plan_art(world, WorldArt(), here=None, setting="x")]
    assert "layout:farm" in keys and "layout:house" in keys and "layout:town" not in keys
    # Land round every place the story knows of, and along the way between them.
    assert "terrain:farm" in keys and "terrain:town" in keys
    along = [k for k in keys if k.startswith("terrain:farm:town:")]
    # Every WAY_PATCH_SIDE along the five miles, except where the town's own
    # land (six km wide) already covers the way.
    assert len(along) == 3 and WAY_PATCH_SIDE == 1600.0
    assert "terrain:hills" not in keys and "layout:hills" not in keys  # country, not a spot


def test_land_along_a_way_never_counts_as_its_first_place_s_own():
    """A road patch is kept under the way's first place; if that place's own
    land failed and the road's came in first, the place still gets its own."""
    from sealedlore.models.world import TerrainPatch

    world = doomsville()
    jobs = plan_art(world, WorldArt(), here=None, setting="x")
    road = next(j for j in jobs if j.key.startswith("terrain:farm:town:"))
    assert road.subject_id == "farm" and road.patch is not None
    x, y, side = road.patch
    art = WorldArt(terrain=[TerrainPatch(place_id="farm", x=x, y=y, side=side, rows=["."])])
    keys = [j.key for j in plan_art(world, art, here=None, setting="x")]
    assert "terrain:farm" in keys and road.key not in keys


def test_a_building_the_story_changed_is_planned_anew_though_nobody_went_in():
    """The barn blown up while Jane stood in the yard: its plan is drawn in
    its new state (an edit of the old, with fire offered), so the local map
    shows the ruin; an unchanged building nobody entered gets none."""
    world = doomsville()
    barn = next(p for p in world.places.values() if p.kind == "building" and p.id != "house")
    world.places[barn.id] = barn.model_copy(update={"state": "blown up and burning"})
    keys = [j.key for j in plan_art(world, WorldArt(), here="house", setting="x")]
    assert f"layout:{barn.id}:blown up and burning" in keys
    before = PlaceLayout(place_id=barn.id, rows=["#"], spec={"features": []})
    jobs = plan_art(world, WorldArt(layouts={barn.id: {"": before}}), here="house", setting="x")
    job = next(j for j in jobs if j.key == f"layout:{barn.id}:blown up and burning")
    assert '"fill": "fire"' in job.messages[-1].text
    world.places[barn.id] = barn  # unchanged again: no plan of its own
    keys = [j.key for j in plan_art(world, WorldArt(), here="house", setting="x")]
    assert not any(k.startswith(f"layout:{barn.id}") for k in keys)


def test_a_place_that_isn_t_a_building_is_told_how_to_lay_out_a_station():
    """Live, Deep Space 9 came back as rooms floating in open space."""
    world = doomsville()
    jobs = {j.key: j for j in plan_art(world, WorldArt(), here="house", setting="x")}
    farm, house = jobs["layout:farm"].messages[-1].text, jobs["layout:house"].messages[-1].text
    assert "space station, a ship or a sealed habitat" in farm
    assert "space station" not in house and "inside of the building" in house


def test_the_map_s_progress_is_what_the_running_threads_have_left(tmp_path: Path, story, cast):
    import threading

    session = StorySession(
        StoryBundle(story=story, cast=cast),
        Config(),
        MockChatProvider(),
        root=tmp_path,
        learn_corrections=False,
    )
    assert session.map_art_progress() == (0, 0)
    release = threading.Event()
    session._art_thread = threading.Thread(target=release.wait)
    session._art_thread.start()
    session._art_total, session._art_done = 5, 2
    session._picture_total, session._picture_done = 3, 1  # its thread isn't running
    assert session.map_art_progress() == (2, 5)
    release.set()
    session._art_thread.join()
    assert session.map_art_progress() == (0, 0)


def test_land_is_drawn_round_the_local_area_not_the_country_it_is_in():
    """The caves in the hills: land round the caves (the hills are wider
    than a local area), never a 12 km patch of hills that would cover the
    road to town and keep it from its own land."""
    world = doomsville()
    world.visited = {"Jane": ["caves"]}
    world.where = {"Jane": "caves"}
    keys = [j.key for j in plan_art(world, WorldArt(), here="caves", setting="x")]
    assert "terrain:caves" in keys and "terrain:hills" not in keys
    assert "layout:caves" in keys and "layout:hills" not in keys


def test_land_on_land_already_drawn_is_dropped():
    from sealedlore.models.world import TerrainPatch

    art = WorldArt()
    adopt(art, TerrainPatch(place_id="farm", x=0, y=0, side=1600, rows=["."]))
    adopt(art, TerrainPatch(place_id="farm", x=500, y=0, side=1600, rows=["."]))
    adopt(art, TerrainPatch(place_id="farm", x=1600, y=0, side=1600, rows=["."]))
    assert [(p.x, p.y) for p in art.terrain] == [(0, 0), (1600, 0)]
    # A small patch (a creek crossing's 400 m) never keeps larger land away.
    adopt(art, TerrainPatch(place_id="creek", x=3000, y=0, side=400, rows=["."]))
    adopt(art, TerrainPatch(place_id="road", x=3000, y=100, side=1600, rows=["."]))
    assert [p.side for p in art.terrain] == [1600, 1600, 400, 1600]


# --- what needs drawing ----------------------------------------------------------


def farm_world() -> WorldMap:
    farm = WorldPlace(id="farm", name="Farmhouse", kind="building")
    yard = WorldPlace(id="yard", name="Yard", kind="area", inside="farm")
    hills = WorldPlace(id="hills", name="The hills", kind="region")
    truck = WorldThing(id="truck", name="Pickup truck", at="farm", state="rusty")
    delta = WorldDelta(places=[farm, yard, hills], things=[truck], where={"Jane": "farm"})
    place_delta(WorldMap(), delta, "Jane")
    return applied(WorldMap(), delta)


def test_the_story_s_places_get_land_and_a_layout_and_sprites_nearest_first():
    jobs = plan_art(farm_world(), WorldArt(), here="farm", setting="a frozen winter")
    keys = [job.key for job in jobs]
    assert keys[0] == "terrain:farm"
    assert keys[1] == "layout:farm"
    assert "sprite:farm" in keys and "sprite:truck:rusty" in keys
    assert "sprite:yard" not in keys and "sprite:hills" not in keys  # drawn as land
    assert "layout:hills" not in keys  # never visited
    ask = jobs[0].messages[-1].text
    assert "Draw the land around: Farmhouse, a building" in ask and "a frozen winter" in ask
    layout = jobs[1].messages
    assert "One tile is about" in layout[0].text
    assert "- Yard (area): x " in layout[1].text  # placed, so told where it stays
    assert "Pickup truck (rusty)" in layout[1].text


def test_what_is_drawn_is_never_asked_again_nor_what_keeps_failing():
    world = farm_world()
    art = WorldArt(
        layouts={"farm": {"": PlaceLayout(place_id="farm", rows=["."])}},
        sprites=[
            WorldSprite(subject="place", subject_id="farm", name="Farmhouse", svg="<svg/>"),
            WorldSprite(
                subject="thing",
                subject_id="truck",
                name="Pickup truck",
                state="rusty",
                svg="<svg/>",
            ),
        ],
        failures={"terrain:farm": 2},
    )
    assert plan_art(world, art, here="farm", setting="x") == []


def test_a_changed_thing_is_drawn_as_an_edit_of_its_last_sprite():
    world = farm_world()
    world.things["truck"].state = "burnt out"
    first = WorldSprite(
        subject="thing", subject_id="truck", name="Pickup truck", state="rusty", svg="<svg>A</svg>"
    )
    art = WorldArt(
        sprites=[first], layouts={"farm": {"": PlaceLayout(place_id="farm", rows=["."])}}
    )
    (job,) = [j for j in plan_art(world, art, here="farm", setting="x") if j.subject_id == "truck"]
    assert job.key == "sprite:truck:burnt out" and job.from_sprite == first.id
    roles = [m.role for m in job.messages]
    assert roles == ["system", "user", "assistant", "user"]
    assert "<svg>A</svg>" in job.messages[2].text
    assert "Now draw the same Pickup truck, burnt out." in job.messages[3].text


def test_a_finished_drawing_is_checked_and_kept_once():
    sprite = finish(
        ArtJob("sprite:truck:rusty", "sprite", [], "truck", name="Pickup truck", subject="thing"),
        TRUCK,
        model="art/model",
        made_at="now",
    )
    assert isinstance(sprite, WorldSprite) and "<rect" in sprite.svg
    with pytest.raises(ValueError):
        finish(ArtJob("sprite:x", "sprite", [], "x"), "I can't draw that.", model="m", made_at="t")
    land = finish(
        ArtJob("terrain:farm", "terrain", [], "farm", patch=(0.0, 0.0, 400.0)),
        json.dumps(
            {
                "ground": "snow",
                "features": [
                    {"kind": "area", "fill": "trees", "x": 0, "y": 0, "w": 5, "h": 5},
                    {"kind": "building", "x": 10, "y": 10, "w": 5, "h": 5},
                ],
            }
        ),
        model="m",
        made_at="t",
    )
    assert len(land.rows) == TERRAIN_TILES and "#" not in "".join(land.rows)  # land only
    art = WorldArt()
    adopt(art, land)
    adopt(art, land.model_copy(update={"id": "other", "rows": ["~"]}))
    assert len(art.terrain) == 1 and art.terrain[0].rows == land.rows  # never redrawn


# --- in the session ---------------------------------------------------------------

PASSAGE = "Serrik shouldered the cellar door open and climbed up into the cold yard."
READ = json.dumps(
    {
        "places": [{"name": "The yard", "kind": "area", "quote": "climbed up into the cold yard"}],
        "where": [{"who": "Serrik", "place": "The yard", "quote": "climbed up into the cold yard"}],
    }
)
LAND = json.dumps(
    {
        "ground": "snow",
        "features": [{"kind": "area", "fill": "trees", "x": 0, "y": 0, "w": 9, "h": 9}],
    }
)


def make_session(tmp_path: Path, story, cast, responses, *, art: bool = True) -> StorySession:
    story.defaults.main_model = "anthropic/claude-sonnet-4.5"
    story.defaults.world_map = True
    story.held_character_id = "char-serrik"
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1")],
        active_provider_name="nano",
        min_cacheable_tokens=1,
        scene_model="small/scene",
        map_art_model="art/model",
        scene_reads="manual",
        map_art=art,
    )
    return StorySession(
        bundle,
        config,
        MockChatProvider(responses),
        root=tmp_path,
        estimator=TokenEstimator(counter=fallback_counter),
        learn_corrections=False,
    )


def turn() -> TurnRequest:
    return TurnRequest(
        speaker_id="char-serrik", user_text="I climb out.", controlled_character_id="char-serrik"
    )


def test_after_a_read_the_map_is_drawn_and_kept(tmp_path: Path, story, cast):
    # The passage, the read, then the land and the yard's layout (an area: no sprite).
    session = make_session(tmp_path, story, cast, [PASSAGE, READ, LAND, LAND])
    list(session.send(turn()))

    art_requests = [r for r in session.provider.requests if r.model == "art/model"]
    assert len(art_requests) == 2
    kept = load_world_art(session.story.id, tmp_path)
    assert len(kept.terrain) == 1 and len(kept.layouts) == 1
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=tmp_path)]
    assert kinds.count("map_art_request") == 2 and kinds.count("map_art_response") == 2
    assert session.map_art_jobs() == []  # nothing left to draw


def test_a_failed_drawing_is_counted_and_let_be(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [PASSAGE, READ, "no", "no"])
    list(session.send(turn()))
    assert session.world_art.failures == {
        f"terrain:{next(iter(session.world().places))}": 1,
        f"layout:{next(iter(session.world().places))}": 1,
    }
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=tmp_path)]
    assert kinds.count("map_art_refused") == 2


def test_with_drawing_off_nothing_is_drawn(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [PASSAGE, READ], art=False)
    list(session.send(turn()))
    assert all(r.model != "art/model" for r in session.provider.requests)
    assert session.map_art_jobs() == []


def test_nothing_is_drawn_in_a_private_scene(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, [PASSAGE, READ], art=False)
    list(session.send(turn()))
    session.config.map_art = True
    session.enter_private(keep="memory", provider=MockChatProvider(["x"]), model="local/m")
    assert session.map_art_jobs() == []
    assert not session.start_map_art()


def test_a_backup_and_a_copy_carry_the_art(tmp_path: Path, story, cast):
    from sealedlore.storage.repository import copy_story

    session = make_session(tmp_path, story, cast, [PASSAGE, READ, LAND, LAND])
    list(session.send(turn()))
    copy = copy_story(session.story.id, title="Copy", root=tmp_path)
    assert len(load_world_art(copy.story.id, tmp_path).layouts) == 1


def test_land_already_drawn_is_never_drawn_again_under_a_neighbour():
    farm = WorldPlace(id="farm", name="Farmhouse", kind="building", x=0.0, y=0.0)
    barn = WorldPlace(id="barn", name="Barn", kind="building", x=60.0, y=0.0)
    town = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=8000.0)
    world = WorldMap(
        places={"farm": farm, "barn": barn, "town": town},
        visited={"Jane": ["farm", "barn", "town"]},
    )
    keys = [j.key for j in plan_art(world, WorldArt(), here="farm", setting="x")]
    land = [k for k in keys if k.startswith("terrain:")]
    assert "terrain:town" in land
    assert len([k for k in land if k in ("terrain:farm", "terrain:barn")]) == 1


def test_a_barn_blown_up_is_drawn_blown_up_as_an_edit_of_itself():
    from sealedlore.engine.world import resolve_reply

    barn = WorldPlace(id="barn", name="Barn", kind="building", x=0.0, y=0.0)
    world = WorldMap(places={"barn": barn}, visited={"Jane": ["barn"]})
    intact = WorldSprite(subject="place", subject_id="barn", name="Barn", svg="<svg>BARN</svg>")
    plan = {"ground": "dirt", "features": [{"kind": "building", "x": 1, "y": 1, "w": 6, "h": 4}]}
    art = WorldArt(
        sprites=[intact],
        layouts={"barn": {"": PlaceLayout(place_id="barn", spec=plan, rows=["."])}},
    )
    source = "The charge went off and the barn blew apart, its roof falling in."
    delta, refused = resolve_reply(
        world,
        {"places": [{"name": "Barn", "state": "blown up", "quote": "the barn blew apart"}]},
        source=source,
        people=[],
    )
    assert refused == []
    world = applied(world, delta)
    assert world.places["barn"].state == "blown up"
    assert (world.places["barn"].x, world.places["barn"].y) == (0.0, 0.0)  # it doesn't move

    jobs = {j.key: j for j in plan_art(world, art, here="barn", setting="x")}
    sprite, layout = jobs["sprite:barn:blown up"], jobs["layout:barn:blown up"]
    assert sprite.from_sprite == intact.id and "<svg>BARN</svg>" in sprite.messages[2].text
    assert "Now draw the same Barn, blown up." in sprite.messages[-1].text
    assert json.loads(layout.messages[2].text) == plan
    assert "Now lay out the same Barn, blown up. Keep what still stands" in layout.messages[3].text

    # Drawn, both states are kept: a branch where it still stands shows it standing.
    adopt(art, finish(sprite, TRUCK, model="m", made_at="t"))
    adopt(art, finish(layout, json.dumps(plan), model="m", made_at="t"))
    assert art.sprite_for("barn", "").svg == "<svg>BARN</svg>"
    assert art.sprite_for("barn", "blown up").from_sprite == intact.id
    assert set(art.layouts["barn"]) == {"", "blown up"}
    left = [j.key for j in plan_art(world, art, here="barn", setting="x")]
    assert left == ["terrain:barn"]  # the land stays; the ruin is in the barn's plan


def test_art_a_bundle_carries_is_written_with_the_story_and_a_restart_keeps_it(
    tmp_path: Path, story, cast
):
    from sealedlore.models.world import TerrainPatch
    from sealedlore.storage.scenario import (
        bundle_from_scenario,
        fresh_playthrough,
        scenario_from_bundle,
    )
    from sealedlore.storage.world_art import load_sidecar_art, save_world_art, sidecar_art_path

    story.defaults.world_map = True
    art = WorldArt(terrain=[TerrainPatch(place_id="farm", x=0, y=0, side=1600, rows=["."])])
    bundle = StoryBundle(story=story, cast=cast, world_art=art)
    save_story_bundle(bundle, root=tmp_path)
    assert bundle.world_art is None  # written, not kept on the bundle
    assert load_world_art(story.id, tmp_path).terrain[0].place_id == "farm"
    # A restart carries the art it had; a scenario file keeps it too.
    again = fresh_playthrough(bundle, "again", None, load_world_art(story.id, tmp_path))
    assert again.world_art is not None and again.world_art.terrain[0].side == 1600
    scenario = scenario_from_bundle(bundle, None, art)
    assert bundle_from_scenario(scenario).world_art is not None
    # A plot file's art beside it is read by name; a missing or broken one is none.
    plot = tmp_path / "Farm.md"
    plot.write_text("# World\n", encoding="utf-8")
    assert load_sidecar_art(plot) is None
    save_world_art("x", art, tmp_path)
    sidecar_art_path(plot).write_bytes((tmp_path / "stories" / "x" / "world_art.json").read_bytes())
    assert load_sidecar_art(plot).terrain[0].place_id == "farm"
    sidecar_art_path(plot).write_text("{not json", encoding="utf-8")
    assert load_sidecar_art(plot) is None


def test_a_restart_or_a_scenario_keeps_the_map_pictures_with_their_files(
    tmp_path: Path, story, cast
):
    """What was drawn for a story stays drawn in its new playthrough and in
    its scenario file: map pictures come with their files. The day's count
    and the failures are the old story's own; a picture whose file is gone
    is left out, or it would never be drawn again."""
    from sealedlore.models.world import MapPicture
    from sealedlore.storage.images import read_story_file, write_story_file
    from sealedlore.storage.scenario import (
        bundle_from_scenario,
        fresh_playthrough,
        read_scenario,
        scenario_from_bundle,
        write_scenario,
    )
    from sealedlore.storage.world_art import carried_story_art, save_world_art
    from tests.test_world_pictures import png

    story.defaults.world_map = True
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    write_story_file(story.id, "images/map/jane.png", png(), tmp_path)
    art = WorldArt(
        pictures=[
            MapPicture(
                subject="person", subject_id="Jane", name="Jane", file="images/map/jane.png"
            ),
            MapPicture(subject="place", subject_id="barn", name="Barn", file="images/map/gone.png"),
        ],
        pictures_by_day={"2026-10-14": 12},
        failures={"layout:farm": 3},
    )
    save_world_art(story.id, art, tmp_path)
    carried, files = carried_story_art(story.id, tmp_path)
    assert [p.subject_id for p in carried.pictures] == ["Jane"]
    assert set(files) == {"images/map/jane.png"}
    assert carried.pictures_by_day == {} and carried.failures == {}
    # A new playthrough writes the picture's file beside its record.
    again = fresh_playthrough(StoryBundle(story=story, cast=cast), "again", files, carried)
    save_story_bundle(again, root=tmp_path)
    assert load_world_art(again.story.id, tmp_path).pictures[0].file == "images/map/jane.png"
    assert read_story_file(again.story.id, "images/map/jane.png", tmp_path) is not None
    # A scenario file carries it too; a record whose file isn't in the file is dropped.
    path = tmp_path / "farm.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(again, files, carried))
    scenario = read_scenario(path)
    assert bundle_from_scenario(scenario).world_art.pictures[0].subject_id == "Jane"
    scenario.reference_files.clear()
    assert bundle_from_scenario(scenario).world_art.pictures == []


def test_art_read_from_a_file_is_cleaned_as_drawn_art_is(tmp_path: Path):
    """A scenario's or a sample's art may be anyone's: its SVG is rebuilt
    from plain shapes when the story loads it, as drawn art always is. A
    sample's art beside its plot file never brings pictures or counts."""
    from sealedlore.engine.world_art import cleaned_art
    from sealedlore.models.world import MapPicture, WorldSprite
    from sealedlore.storage.world_art import load_sidecar_art, sidecar_art_path

    rect = '<rect x="1" y="1" width="5" height="5" />'
    bad = f'<svg>{rect}<image href="file:///etc/passwd"/><script>x</script></svg>'
    art = cleaned_art(
        WorldArt(
            sprites=[
                WorldSprite(subject="place", subject_id="barn", name="Barn", svg=bad),
                WorldSprite(
                    subject="place",
                    subject_id="nothing",
                    name="N",
                    svg="<svg><script>x</script></svg>",
                ),
            ]
        )
    )
    assert [s.subject_id for s in art.sprites] == ["barn"]
    assert "image" not in art.sprites[0].svg and "script" not in art.sprites[0].svg
    assert cleaned_art(art.model_copy(deep=True)) == art  # clean art comes back the same
    plot = tmp_path / "Farm.md"
    sidecar_art_path(plot).write_text(
        WorldArt(
            pictures=[
                MapPicture(subject="person", subject_id="J", name="J", file="images/map/j.png")
            ],
            pictures_by_day={"2026-10-14": 3},
            failures={"x": 1},
        ).model_dump_json(),
        encoding="utf-8",
    )
    shipped = load_sidecar_art(plot)
    assert shipped is not None
    assert (shipped.pictures, shipped.pictures_by_day, shipped.failures) == ([], {}, {})


def test_a_town_gets_a_finer_grid_and_a_building_s_plan_is_its_inside():
    from sealedlore.engine.world_art import plan_grid

    world = doomsville()
    assert plan_grid(world.places["town"]) == (80, 48) and plan_grid(world.places["farm"]) == (
        40,
        24,
    )
    west, north, width, height = plan_rect(world, world.places["town"])
    assert (width, height) == (1500.0, 900.0)  # 18.75 m tiles, not 37.5
    world.visited = {"Jane": ["town", "house"]}
    jobs = {j.key: j for j in plan_art(world, WorldArt(), here="town", setting="x")}
    assert "80 tiles wide" in jobs["layout:town"].messages[0].text
    assert jobs["layout:town"].extra["grid"] == (80, 48)
    assert "This is the inside of the building" in jobs["layout:house"].messages[1].text
    assert "inside of the building" not in jobs["layout:farm"].messages[1].text
    plan = finish(
        jobs["layout:town"], json.dumps({"ground": "dirt", "features": []}), model="m", made_at="t"
    )
    assert len(plan.rows) == 48 and len(plan.rows[0]) == 80


def test_land_is_told_the_ground_of_the_land_drawn_nearest():
    from sealedlore.models.world import TerrainPatch

    world = doomsville()
    art = WorldArt(
        terrain=[TerrainPatch(place_id="farm", x=0, y=0, side=1600, rows=["."], ground="snow")]
    )
    jobs = {j.key: j for j in plan_art(world, art, here="house", setting="a dead land")}
    ask = jobs["terrain:town"].messages[1].text
    assert "a dead land. The land already drawn nearest here is snow" in ask


def test_a_deck_s_blank_inside_its_hull_and_its_narrow_gaps_are_floor():
    """Deep Space 9 (the author, Oct 15 2026): rooms joined by nothing but
    space, people standing in space. Space outside the hull stays space."""
    from sealedlore.engine.world_art import decked, standing_spot

    rows = (
        "          ",
        " ######## ",
        " #__ ___# ",
        " #      # ",
        " #__  __# ",
        " ######## ",
        "          ",
        "          ",
        "          ",
        " ##  ##   ",
    )
    deck = decked(rows)
    assert deck[2] == " #______# "  # a one-tile gap: floor
    assert deck[3] == " #______# " and deck[4] == " #______# "  # enclosed: floor
    assert deck[0] == "          " and deck[7] == "          "  # outside: space
    assert deck[9] == " ##__##   "  # a two-tile gap between rooms: a corridor
    # Someone placed on space or a wall stands on the nearest floor instead.
    assert standing_spot(rows, (3, 3)) in {(3, 2), (3, 4)}
    assert standing_spot(deck, (3, 3)) == (3, 3)
    assert standing_spot(deck, (1, 3)) == (2, 3)
