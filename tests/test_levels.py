"""Levels of a place's plan (engine/levels.py, world_art.py's level jobs,
area.py `build_level`, session_area.py's stairs): a two-storey house, a
plan that lists levels only when its words call for them, stairs as the
doors between levels, the storyteller told the level, furnishing per level,
old art files with no levels, and the level job's reply filed on its plan."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sealedlore.engine.area import build_area, build_level, render_positions
from sealedlore.engine.levels import (
    level_area_id,
    levels_called_for,
    linked_levels,
    listed_levels,
    parse_level,
    place_of_area,
    stairs_spot,
)
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.world_art import (
    ArtJob,
    adopt,
    finish,
    has_levels,
    layout_messages,
    plan_art,
    rasterise,
)
from sealedlore.models.area import Spot
from sealedlore.models.config import Config, ProviderConfig
from sealedlore.models.world import (
    LevelLayout,
    PlaceLayout,
    WorldArt,
    WorldDelta,
    WorldMap,
    WorldPlace,
)
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.repository import StoryBundle, save_story_bundle
from sealedlore.storage.world_art import save_world_art
from tests.test_area import FARM_SPEC

# The house's ground floor: one building filling the grid, a wall across it
# with a door, stairs at (10, 12), and an upper floor listed.
GROUND_SPEC = {
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
        {"kind": "wall", "points": [[20, 0], [20, 23]], "gaps": [[20, 12]]},
        {"kind": "stairs", "x": 10, "y": 12, "w": 1, "h": 2},
        {"kind": "object", "name": "a cast-iron stove", "x": 25, "y": 3},
    ],
    "levels": [{"index": 1, "name": "Upper floor"}],
}
# The upper floor: the same outline, the stairs in the same tiles, two rooms.
UPPER_SPEC = {
    "ground": "floor",
    "features": [
        {"kind": "building", "x": 0, "y": 0, "w": 40, "h": 24, "doors": []},
        {"kind": "wall", "points": [[20, 0], [20, 23]], "gaps": [[20, 6]]},
        {"kind": "stairs", "x": 10, "y": 12, "w": 1, "h": 2},
        {"kind": "object", "name": "a four-poster bed", "x": 30, "y": 5},
    ],
}


def house_place() -> WorldPlace:
    return WorldPlace(
        id="house",
        name="The house",
        kind="building",
        inside="farm",
        x=20.0,
        y=30.0,
        size=40.0,
        what="a two-storey farmhouse with a steep roof",
    )


def world_and_art(*, upper: bool = True) -> tuple[WorldMap, WorldArt]:
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400.0)
    world = WorldMap(places={"farm": farm, "house": house_place()}, seen=["farm", "house"])
    art = WorldArt()
    rows, objects, labels = rasterise(FARM_SPEC, 40, 24)
    art.layouts["farm"] = {
        "": PlaceLayout(place_id="farm", spec=FARM_SPEC, rows=rows, objects=objects, made_at="1")
    }
    rows, objects, labels = rasterise(GROUND_SPEC, 40, 24)
    plan = PlaceLayout(
        place_id="house", spec=GROUND_SPEC, rows=rows, objects=objects, labels=labels, made_at="1"
    )
    if upper:
        rows, objects, labels = rasterise(UPPER_SPEC, 40, 24)
        plan.levels = [
            LevelLayout(
                index=1,
                name="Upper floor",
                place_id="house",
                spec=UPPER_SPEC,
                rows=rows,
                objects=objects,
                labels=labels,
                made_at="2",
            )
        ]
    art.layouts["house"] = {"": plan}
    return world, art


# --- the levels themselves -----------------------------------------------------


def test_a_level_s_area_id_is_the_place_s_with_its_index_and_the_plan_s_own_is_the_place():
    assert level_area_id("house", 0) == "house"
    assert level_area_id("house", 1) == "house#1"
    assert level_area_id("house", -2) == "house#-2"
    assert parse_level("house#1") == ("house", 1)
    assert parse_level("house#-2") == ("house", -2)
    assert parse_level("house") is None and parse_level("house#0") is None
    assert parse_level("window:0:1600:1600") is None
    assert place_of_area("house#3") == "house" and place_of_area("farm") == "farm"


def test_levels_are_asked_for_only_when_the_place_s_words_call_for_them():
    plain = WorldPlace(id="b", name="The barn", kind="building", what="a red barn")
    storeys = house_place()
    station = WorldPlace(id="s", name="Deep Station", kind="site", what="a station of three decks")
    yard = WorldPlace(id="y", name="The yard", kind="site", what="a yard with a cellar door")
    assert not levels_called_for(plain)
    assert levels_called_for(storeys)
    assert levels_called_for(station)  # enclosed by its words, whatever its kind
    assert not levels_called_for(yard)  # a site's grounds have no upper floor
    world, _art = world_and_art()
    asked = layout_messages(world, storeys, "a farm", DEFAULT_TEXTS)
    assert '"levels"' in asked[-1].text
    quiet = layout_messages(world, plain, "a farm", DEFAULT_TEXTS)
    assert '"levels"' not in quiet[-1].text
    # The listing is held to the rules: no 0, whole indexes in range, one each.
    spec = {"levels": [{"index": 1, "name": "Upper"}, {"index": 0}, {"index": 1}, 2, {"index": 9}]}
    assert listed_levels(spec) == [(1, "Upper"), (2, "Level 2")]


def test_plan_art_plans_a_job_per_level_only_when_the_plan_lists_them():
    world, art = world_and_art(upper=False)
    world.visited = {"Serrik Vaun": ["house"]}
    jobs = plan_art(world, art, here="house", setting="a farm")
    level_jobs = [j for j in jobs if ":level:" in j.key]
    assert [j.key for j in level_jobs] == ["layout:house:level:1"]
    job = level_jobs[0]
    assert job.extra["level"] == 1 and job.extra["level_name"] == "Upper floor"
    # The plan's own request and shapes stand before the level's ask, which
    # names the stairs tiles to keep.
    assert job.messages[-2].role == "assistant" and '"levels"' in job.messages[-2].text
    assert "x 10, y 12" in job.messages[-1].text and "Upper floor" in job.messages[-1].text
    # Drawn: not asked again; failed twice: let be.
    world2, art2 = world_and_art(upper=True)
    world2.visited = {"Serrik Vaun": ["house"]}
    assert not [j for j in plan_art(world2, art2, here="house", setting="s") if ":level:" in j.key]
    art.failures["layout:house:level:1"] = 2
    assert not [j for j in plan_art(world, art, here="house", setting="s") if ":level:" in j.key]
    # A plan that lists none, or a place whose words don't call for levels, plans none.
    art.failures.clear()
    art.layouts["house"][""].spec = {**GROUND_SPEC, "levels": []}
    assert not [j for j in plan_art(world, art, here="house", setting="s") if ":level:" in j.key]
    assert not has_levels(
        WorldPlace(id="house", name="The house", kind="building"), art.layouts["house"][""]
    )


def test_a_level_job_s_reply_is_finished_into_the_plan_s_levels():
    world, art = world_and_art(upper=False)
    job = ArtJob(
        "layout:house:level:1",
        "layout",
        [],
        "house",
        name="The house, Upper floor",
        extra={"grid": (40, 24), "level": 1, "level_name": "Upper floor"},
    )
    drawn = finish(
        job, json.dumps({**UPPER_SPEC, "levels": [{"index": 2}]}), model="m", made_at="t"
    )
    assert isinstance(drawn, LevelLayout)
    assert drawn.index == 1 and drawn.name == "Upper floor" and drawn.place_id == "house"
    assert "levels" not in drawn.spec  # a level lists none
    assert drawn.rows[12][10] == "&" and len(drawn.rows) == 24
    adopt(art, drawn)
    plan = art.layouts["house"][""]
    assert plan.level(1) is drawn and plan.level(2) is None
    # Drawn again, it replaces the one it had; for a plan that went, it is dropped.
    adopt(art, drawn.model_copy(update={"made_at": "later"}))
    assert len(plan.levels) == 1 and plan.level(1).made_at == "later"
    adopt(art, drawn.model_copy(update={"place_id": "gone"}))
    assert "gone" not in art.layouts
    # A main plan that lists levels but drew no stairs gets one tile of them.
    bare = {**GROUND_SPEC, "features": GROUND_SPEC["features"][:2]}
    main = finish(
        ArtJob(
            "layout:house", "layout", [], "house", extra={"grid": (40, 24), "levels_asked": True}
        ),
        json.dumps(bare),
        model="m",
        made_at="t",
    )
    assert isinstance(main, PlaceLayout)
    assert any("&" in row for row in main.rows)
    assert any(f.get("added_by") == "program" for f in main.spec["features"])
    assert stairs_spot(["#####", "#___#", "#___#", "#####"]) == (2, 1)


def test_a_plan_with_a_level_is_two_areas_joined_by_their_stairs():
    world, art = world_and_art()
    ground = build_area(world, art, "house")
    upper = build_level(world, art, "house", 1)
    assert ground is not None and upper is not None
    assert ground.id == "house" and upper.id == "house#1"
    assert upper.level == 1 and upper.level_name == "Upper floor" and upper.indoor
    assert upper.title == "The house · Upper floor" and ground.title == "The house"
    up = ground.stairs_entrances()
    down = upper.stairs_entrances()
    assert {(e.x, e.y) for e in up} == {(10, 12), (10, 13)}
    assert all(e.place_id == "house#1" and e.side == "up" for e in up)
    assert {(e.x, e.y) for e in down} == {(10, 12), (10, 13)}
    assert all(e.place_id == "house" and e.side == "down" for e in down)
    assert ground.walkable(10, 12) and upper.walkable(10, 12)
    # The upper floor has rooms of its own and no door out but the stairs.
    assert upper.rooms and upper.exits == []
    assert build_level(world, art, "house", 2) is None


def test_a_level_without_stairs_to_the_level_below_gets_them_by_the_rules():
    world, art = world_and_art()
    plan = art.layouts["house"][""]
    # The upper floor drew its stairs elsewhere: it gets stairs over the
    # ground floor's, and says so.
    rows, objects, labels = rasterise(
        {
            **UPPER_SPEC,
            "features": [*UPPER_SPEC["features"][:2], {"kind": "stairs", "x": 30, "y": 20}],
        },
        40,
        24,
    )
    plan.levels[0].rows = rows
    links = linked_levels(plan)
    assert links.links[1][(10, 12)] == 0 and links.links[0][(10, 12)] == 1
    assert any("over level 0's" in r for r in links.repairs)
    upper = build_level(world, art, "house", 1)
    assert upper is not None and any(e.place_id == "house" for e in upper.stairs_entrances())
    # Neither drew stairs: both get a tile of them on floor they share.
    rows, _o, _l = rasterise({**GROUND_SPEC, "features": GROUND_SPEC["features"][:2]}, 40, 24)
    plan.rows = rows
    rows, _o, _l = rasterise({**UPPER_SPEC, "features": UPPER_SPEC["features"][:2]}, 40, 24)
    plan.levels[0].rows = rows
    links = linked_levels(plan)
    assert links.links[1] and set(links.links[1].values()) == {0}
    assert any("added stairs between" in r for r in links.repairs)
    tile = next(iter(links.links[1]))
    assert links.rows[0][tile[1]][tile[0]] == "&" and links.rows[1][tile[1]][tile[0]] == "&"
    # A level drawn on the wrong grid is held to the plan's.
    plan.levels[0].rows = ["#" * 50] * 30
    assert len(linked_levels(plan).rows[1]) == 24 and len(linked_levels(plan).rows[1][0]) == 40


def test_an_old_art_file_with_no_levels_loads_and_a_plan_without_them_is_unchanged():
    art = WorldArt.model_validate(
        {"layouts": {"house": {"": {"place_id": "house", "rows": ["###", "#_#", "###"]}}}}
    )
    plan = art.layouts["house"][""]
    assert plan.levels == [] and plan.level(1) is None
    world, art = world_and_art(upper=False)
    ground = build_area(world, art, "house")
    assert ground is not None and ground.stairs_entrances() == []
    assert not any("stairs" in r for r in ground.repairs)


# --- the session ---------------------------------------------------------------


def make_session(tmp_path: Path, story, cast, responses=()) -> StorySession:
    world, art = world_and_art()
    story.defaults.world_map = True
    story.world_base = WorldDelta(places=list(world.places.values()), seen=list(world.seen))
    story.world_start = "house"
    story.held_character_id = "char-serrik"
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


def climb(session: StorySession) -> None:
    """Walk the author's character onto the stairs and up them."""
    me = session.area_state().spots["Serrik Vaun"]
    assert me.area == "house"
    steps = session.walk_route(10, 12)
    assert steps and steps[-1] == (10, 12)
    session.walked(steps)
    assert session.door_here() == ("house#1", "The house, upper floor")
    assert session.go_through() is not None


def test_walking_up_the_stairs_lands_a_step_off_them_on_the_floor_above(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    climb(session)
    state = session.area_state()
    me = state.spots["Serrik Vaun"]
    assert me.area == "house#1"
    upper = session.area("house#1")
    assert upper is not None and upper.level == 1
    assert (me.x, me.y) not in {(e.x, e.y) for e in upper.stairs_entrances()}
    assert abs(me.x - 10) + abs(me.y - 12) <= 3
    assert "from The house to The house, upper floor" in state.walked_words
    assert session.here_place() == "house"
    # And down again: the title says the ground floor.
    steps = session.walk_route(10, 12)
    session.walked(steps)
    assert session.door_here() == ("house", "The house")
    session.go_through()
    assert session.area_state().spots["Serrik Vaun"].area == "house"
    # Travel never offers the building one is upstairs in.
    climb(session)
    assert "house" not in {pid for pid, _n, _s in session.travel_choices()}


def test_the_storyteller_is_told_the_level_the_stairs_and_who_is_on_another_level(
    tmp_path, story, cast
):
    session = make_session(tmp_path, story, cast, ["She came up the stairs."])
    climb(session)
    session.story.scene.present_others = [
        "Jane"
    ]  # here, by the scene; the map has her in the house
    state = session.area_state()
    state.spots["Jane"] = Spot(area="house", x=25, y=5)
    session.story.area_live.state = state
    list(session.send(turn("I look out of the window.")))
    sent = session.provider.requests[0].messages[-1].text
    assert "- Serrik Vaun: in The house, upper floor" in sent
    assert "Stairs from here: down to the ground floor" in sent
    also = next(line for line in sent.splitlines() if line.startswith("Also in The house"))
    assert (
        also.startswith("Also in The house, on another level: ") and "Jane (ground floor)" in also
    )
    # The pure rendering, for a level with nothing but its own people.
    world, art = world_and_art()
    upper = build_level(world, art, "house", 1)
    text = render_positions(upper, world, {"Me": Spot(area="house#1", x=12, y=12)}, "Me")
    assert "in The house, upper floor" in text and "down to the level below" in text
    # Someone the map keeps in the house stays on the level they stood on.
    user = session.path()[-2]
    assert user.meta.area.spots["Jane"].area == "house"
    assert user.meta.area.spots["Serrik Vaun"].area == "house#1"


def test_each_level_is_furnished_on_its_own_under_its_area_id(tmp_path, story, cast):
    furnishing = json.dumps(
        {
            "rooms": [
                {
                    "room": 0,
                    "purpose": "bedroom",
                    "items": [{"name": "a wooden bed", "w": 2, "h": 1, "where": "wall"}],
                }
            ]
        }
    )
    session = make_session(tmp_path, story, cast, [furnishing, furnishing])
    session.config.map_art = True
    furnish, _pictures = session.tile_art_jobs()
    assert {a.id for a in furnish} >= {"house", "house#1"}
    upper = next(a for a in furnish if a.id == "house#1")
    session._furnish_one(upper, session.provider)
    art = session.map_art_copy()
    assert "house#1" in art.furnishings and art.furnishings["house#1"][""].place_id == "house#1"
    assert art.furnishing_for("house", "") is None  # the ground floor's own, still to come
    sent = session.provider.requests[-1].messages[-1].text
    assert "The building: The house, upper floor" in sent
    # Once furnished, the level isn't asked again, and its area has the furniture.
    furnish, _pictures = session.tile_art_jobs()
    assert "house#1" not in {a.id for a in furnish}
    placed = session.area("house#1").placements
    assert any(p.name == "a wooden bed" and p.id.startswith("house#1:") for p in placed)


@pytest.mark.skipif(os.environ.get("CI_NO_QT") == "1", reason="needs Qt")
def test_the_stairs_tile_is_drawn_unlike_the_floor():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from sealedlore.engine.tile_sets import TileSetInfo
    from sealedlore.gui.area_render import INDOOR_PX, TileSource, render_area

    QApplication.instance() or QApplication([])
    world, art = world_and_art()
    upper = build_level(world, art, "house", 1)
    plain = TileSource(TileSetInfo(name="plain"), lambda role: None)
    image = render_area(upper, plain, dress=False)
    px = INDOOR_PX

    def tile(x: int, y: int) -> list[tuple[int, int, int]]:
        return [
            image.pixelColor(x * px + dx, y * px + dy).getRgb()[:3]
            for dx in range(4, px - 4, 4)
            for dy in range(4, px - 4, 4)
        ]

    stairs, floor = tile(10, 12), tile(14, 12)
    assert stairs != floor
    assert len(set(stairs)) > 2  # treads: not one flat colour
