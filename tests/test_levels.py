"""Levels of a place's plan (engine/levels.py, world_art.py's level jobs,
area.py `build_level`, session_area.py's stairs): a two-storey house, a
plan that lists levels only when its words call for them, stairs as the
doors between levels, the storyteller told the level, furnishing per level,
old art files with no levels, and the level job's reply filed on its plan."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sealedlore.engine.area import build_area, build_level
from sealedlore.engine.levels import (
    level_area_id,
    linked_levels,
    listed_levels,
    parse_level,
    place_of_area,
)
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import StorySession
from sealedlore.engine.tokens import TokenEstimator, fallback_counter
from sealedlore.engine.world_art import rasterise
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


def test_levels_listed_on_a_stairs_feature_are_read_as_the_plan_s():
    # GLM 5.3 Flash's live reply for a watchtower (Oct 17 2026).
    spec = {
        "ground": "rock",
        "features": [
            {"kind": "building", "name": "The watchtower", "x": 0, "y": 0, "w": 40, "h": 24},
            {
                "kind": "stairs",
                "x": 20,
                "y": 4,
                "w": 1,
                "h": 2,
                "levels": [
                    {"index": 1, "name": "Upper floor"},
                    {"index": 2, "name": "Top of the tower"},
                    {"index": -1, "name": "Cellar"},
                ],
            },
            {"kind": "stairs", "x": 17, "y": 17, "w": 1, "h": 2},
        ],
    }
    assert listed_levels(spec) == [(-1, "Cellar"), (1, "Upper floor"), (2, "Top of the tower")]
    # The object's own list wins when there is one.
    assert listed_levels({**spec, "levels": [{"index": 1, "name": "Loft"}]}) == [(1, "Loft")]


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


