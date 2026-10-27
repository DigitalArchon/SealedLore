"""A visual story's wider world (the author, Oct 26 2026; SealedLoreDocs
design/visual-stories.md §19-20): the overmap's rules (engine/overmap.py),
its cells (engine/cells.py), and play on it (engine/session_overmap.py),
on a small made-up world and on the shipped Doomsville. Never the network."""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from sealedlore.engine import overmap as om_rules  # noqa: E402
from sealedlore.engine.cells import (  # noqa: E402
    AREA_M,
    COLUMNS,
    ROWS,
    cell_of_place,
    cell_place_id,
    lay_pieces,
    make_cell,
)
from sealedlore.engine.keeper import OPS, parse_keeper  # noqa: E402
from sealedlore.engine.visual_rules import warm_garment, warmth  # noqa: E402
from sealedlore.engine.world_art import fixed_in_plan  # noqa: E402
from sealedlore.models.area import ThingRecord  # noqa: E402
from sealedlore.models.kit import OutBuilding, OutdoorTemplate, OutObject  # noqa: E402
from sealedlore.models.overmap import Needs, OverLine, Overmap, OverPlace, OverRegion  # noqa: E402
from sealedlore.models.world import WorldMap  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "SampleStories"


def small_world() -> Overmap:
    """20 x 12 km: quiet fields in the west, a deadly ruin in the east, a
    road across the middle, a river down the west with a bridge."""
    return Overmap(
        west=0,
        east=20,
        south=0,
        north=12,
        regions=[
            OverRegion(
                key="r",
                name="The Ruins",
                boxes=[(12, 20, 0, 12)],
                terrain="ruins",
                chance=90,
                split=(100, 0, 0),
                danger=["a gang"],
                pace=0.5,
            ),
            OverRegion(
                key="f",
                name="The Fields",
                boxes=[(0, 20, 0, 12)],
                terrain="fields",
                chance=0,
                split=(0, 50, 50),
                finds=[("farm", 100)],
                warm=1,
            ),
        ],
        lines=[
            OverLine(kind="road", name="the highway", points=[(0, 6), (20, 6)]),
            OverLine(kind="river", name="the river", points=[(4, 12), (4, 0)]),
        ],
        places=[
            OverPlace(name="Westford", x=1, y=6, known=True, template="farm"),
            OverPlace(name="Eastgate", x=18, y=6, template="farm"),
        ],
    )


FARM = OutdoorTemplate(
    name="A farm",
    what="A farm.",
    buildings=[OutBuilding(name="farmhouse", size=24), OutBuilding(name="barn", size=30)],
    fence=True,
    objects=[OutObject(name="a rusted tractor", w=4, h=2.5, near="buildings")],
    loot=[("junk", 100)],
)


# --- the grid ---------------------------------------------------------------------------


def test_cells_are_counted_from_the_north_west_and_hold_their_points():
    om = small_world()
    assert om_rules.size(om) == (50, 50)
    cell = om_rules.cell_of(om, 1000, 11_900)
    assert cell == (2, 0)
    west, south, east, north = om_rules.cell_rect(om, cell)
    assert west <= 1000 <= east and south <= 11_900 <= north
    assert om_rules.cell_of(om, -10, 5000) is None
    assert om_rules.neighbour((3, 3), "north") == (3, 2)


def test_the_first_region_whose_box_holds_a_point_is_the_region_there():
    om = small_world()
    assert om_rules.region_at(om, 15_000, 5000).key == "r"
    assert om_rules.region_at(om, 5000, 5000).key == "f"


def test_a_road_is_quicker_than_country_and_a_river_slower_off_a_bridge():
    om = small_world()
    on_road = om_rules.route(om, (1000, 6000), (10_000, 6000))
    across = om_rules.route(om, (1000, 2000), (10_000, 2000))
    assert sum(leg.seconds for leg in on_road) < sum(leg.seconds for leg in across)
    assert any(leg.road for leg in on_road)
    assert any(leg.ice for leg in across)  # the river crossed off the bridge


def test_a_stretch_on_a_road_is_walked_through_a_cell_the_road_runs_through():
    om = small_world()
    for leg in om_rules.route(om, (1000, 6000), (10_000, 6000))[1:-1]:
        if leg.road:
            assert om_rules.lines_at(om, leg.cell) & (om_rules.ROAD | om_rules.TRACK)


def _traveller(om: Overmap, **kw) -> om_rules.Traveller:
    base = dict(
        seconds=0,
        start_minute=9 * 60,
        needs=Needs(),
        food=5,
        drink=5,
        warm=1,
        regions={r.key: r for r in om.regions},
    )
    base.update(kw)
    return om_rules.Traveller(**base)


def test_a_long_journey_camps_at_night_and_eats_and_drinks_on_the_way():
    om = small_world()
    om.regions[0].chance = 0  # nothing happens anywhere
    legs = om_rules.route(om, (1000, 6000), (11_000, 1000))
    long_legs = [
        om_rules.Leg(cell=leg.cell, x=leg.x, y=leg.y, seconds=leg.seconds * 6, region=leg.region)
        for leg in legs
    ]
    done = om_rules.journey(long_legs, _traveller(om), random.Random(1), place_name="X")
    assert done.arrived and done.camps >= 1
    assert done.eaten >= 1 and done.drunk >= 1
    assert "You reach X." in done.lines


def test_nothing_is_rolled_in_the_stretch_set_out_from_and_rolls_stop_the_journey():
    om = small_world()
    legs = om_rules.route(om, (13_000, 6000), (19_000, 6000))  # all in the ruins
    done = om_rules.journey(legs, _traveller(om), random.Random(2))
    assert done.stop is not None and done.stop.kind == "danger" and done.stop.leg >= 1


def test_a_journey_is_the_same_for_the_same_moment_and_way():
    om = small_world()
    legs = om_rules.route(om, (1000, 6000), (19_000, 6000))
    a = om_rules.journey(legs, _traveller(om), random.Random("same"))
    b = om_rules.journey(legs, _traveller(om), random.Random("same"))
    assert (a.legs, a.seconds, a.stop) == (b.legs, b.seconds, b.stop)


def test_going_without_hurts_and_the_cold_hurts_the_underdressed():
    om = small_world()
    om.regions[0].chance = 0
    legs = [
        om_rules.Leg(cell=(1, 1), x=1000, y=1000, seconds=8 * 3600, region="f") for _ in range(12)
    ]
    hungry = om_rules.journey(legs, _traveller(om, food=0, drink=0), random.Random(3))
    assert hungry.wounds >= 1
    om.regions[1].warm = 2
    cold = om_rules.journey(legs[:2], _traveller(om, warm=1), random.Random(3))
    assert cold.wounds >= 1 and any("cold" in line for line in cold.lines)


def test_the_estimate_counts_nights_food_and_the_regions_crossed():
    om = small_world()
    legs = om_rules.route(om, (1000, 6000), (19_000, 6000))
    estimate = om_rules.estimate(om, legs, 0, 9 * 60, Needs())
    assert estimate.regions == ["The Fields", "The Ruins"]
    assert estimate.walking > 0 and estimate.road_share > 0.5


def test_setting_out_at_night_with_far_to_go_waits_for_first_light():
    om = small_world()
    om.regions[0].chance = 0
    legs = om_rules.route(om, (1000, 6000), (19_000, 2000))
    done = om_rules.journey(legs, _traveller(om, start_minute=22 * 60), random.Random(4))
    assert done.lines[0] == "You wait for first light before setting out."


# --- warm clothes -------------------------------------------------------------------------


def test_warm_garments_count_once_a_kind():
    assert warm_garment("a wool coat") == "coat" == warm_garment("a down parka")
    assert warm_garment("a pair of winter boots") == "boots"
    assert warm_garment("a flannel shirt") == ""
    carried = [
        ThingRecord(state="taken", name=n)
        for n in ("a wool coat", "a canvas work jacket", "a knit hat", "a tin of beans")
    ]
    assert warmth(carried) == 2


# --- cells ------------------------------------------------------------------------------


def test_a_cell_is_laid_out_with_its_roads_rivers_and_template():
    om = small_world()
    cell = om_rules.cell_of(om, 4000, 6000)  # the river and the road both
    made = make_cell(om, cell, "seed", template_key="farm", template=FARM)
    assert made.place.id == cell_place_id(cell) and cell_of_place(made.place.id) == cell
    assert made.place.size == AREA_M
    kinds = [f["kind"] for f in made.layout.spec["features"]]
    assert "road" in kinds and any(f.get("fill") == "ice" for f in made.layout.spec["features"])
    assert {c.name for c in made.children} <= {"The farmhouse", "The barn"}
    assert all(c.inside == made.place.id and c.kind == "building" for c in made.children)
    assert made.layout.spec["cover"]


def test_a_cells_buildings_stand_in_the_boxes_the_map_draws_them_in():
    om = small_world()
    cell = om_rules.cell_of(om, 8000, 3000)
    made = make_cell(om, cell, "seed", template_key="farm", template=FARM)
    world = WorldMap(places={p.id: p for p in (made.place, *made.children)})
    boxes = fixed_in_plan(world, made.place)
    assert len(boxes) == len(made.children)
    for box in boxes:
        assert 0 <= box["x"] and box["x"] + box["w"] <= COLUMNS
        assert 0 <= box["y"] and box["y"] + box["h"] <= ROWS


def test_the_same_cell_is_made_the_same_for_the_same_story():
    om = small_world()
    cell = (10, 30)
    a = make_cell(om, cell, "story-1", template_key="farm", template=FARM)
    b = make_cell(om, cell, "story-1", template_key="farm", template=FARM)
    assert a.layout.spec == b.layout.spec
    assert [c.model_dump() for c in a.children] == [c.model_dump() for c in b.children]


def test_laid_pieces_never_cut_ground_off():
    rows = ["." * 30 for _ in range(20)]
    rows[10] = "." * 14 + "#" + "." * 15
    objects = [OutObject(name="a crate", w=3, h=3, count=6), OutObject(name="a log", w=4, h=1)]
    pieces, loot = lay_pieces(rows, objects, [("junk", 100)], 1.0, "s", "k")
    assert pieces and loot
    taken = {
        (p["x"] + dx, p["y"] + dy) for p in pieces for dy in range(p["h"]) for dx in range(p["w"])
    }
    free = [
        (x, y) for y in range(20) for x in range(30) if rows[y][x] == "." and (x, y) not in taken
    ]
    seen = {free[0]}
    todo = [free[0]]
    while todo:
        x, y = todo.pop()
        for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if n in set(free) and n not in seen:
                seen.add(n)
                todo.append(n)
    assert len(seen) == len(free)


# --- the keeper's operations ---------------------------------------------------------------


def test_the_keeper_may_bring_in_a_stranger_and_tell_of_a_place():
    assert "stranger" in OPS and "tell_of" in OPS
    reply = parse_keeper(
        '{"script": [{"op": "stranger", "who": "Mara", "card": "A lost woman.", '
        '"attitude": "wary", "where": "nearby"}, {"tell_of": "Stillwater"}], "note": "x"}',
        [],
    )
    assert [op.op for op in reply.script] == ["stranger", "tell_of"]
    assert reply.script[1].get("where") == "Stillwater"


# --- play on Doomsville's overmap ----------------------------------------------------------


@pytest.fixture
def doomsville(tmp_path):
    """A visual story of the shipped Doomsville, made as the start page
    makes it, John played."""
    pytest.importorskip("PySide6")
    from sealedlore.engine.plot_md import parse_plot_markdown
    from sealedlore.engine.session import StorySession
    from sealedlore.models.config import Config
    from sealedlore.providers.mock import MockChatProvider
    from sealedlore.storage.repository import save_story_bundle
    from sealedlore.storage.scenario import bundle_from_scenario
    from sealedlore.storage.visual_design import cards_for_design, load_design
    from sealedlore.storage.world_art import load_sidecar

    sample = SAMPLES / "Doomsville with a map.md"
    parsed = parse_plot_markdown(sample.read_text(), fallback_title="Doomsville")
    bundle = bundle_from_scenario(parsed.scenario, main_model=None, title="Doomsville")
    bundle.world_art, files = load_sidecar(sample)
    bundle.pending_files.update(files)
    story = bundle.story
    story.mode = "visual"
    story.visual_design = load_design(sample)
    names = [c.name for c in (*bundle.cast, *bundle.supporting.characters)]
    bundle.supporting.characters += cards_for_design(story.visual_design, names)
    john = next(c for c in bundle.cast if c.name == "John")
    story.held_character_id = john.id
    story.scene.present_character_ids.append(john.id)
    save_story_bundle(bundle, root=tmp_path)
    session = StorySession.load(story.id, Config(), MockChatProvider(), root=tmp_path)
    session.visual_lay_out()
    return session


def test_doomsville_has_its_wider_world_and_begins_knowing_the_farm_and_the_town(doomsville):
    om = doomsville.overmap()
    assert om is not None and len(om.regions) >= 10
    state = doomsville.area_state()
    assert state.overmap.begun and set(state.overmap.known) == {"The farm", "Hellsville"}
    assert doomsville.overmap_region().name == "Hellsville country"
    names = [c.name for c in doomsville.overmap_choices()]
    assert names[0] == "The farm" and "Hellsville" in names


def test_the_farm_and_the_town_hold_their_own_cells(doomsville):
    om = doomsville.overmap()
    world = doomsville.world()
    farm = next(p for p in world.places.values() if p.name == "The farm")
    cell = om_rules.cell_of(om, farm.x, farm.y)
    assert doomsville.cell_area_id(cell) == farm.id
    assert doomsville.cell_area_id((0, 0)) is None  # not made until entered


def test_a_journey_to_hellsville_takes_hours_on_the_road_and_finds_the_crossing(doomsville):
    for region in doomsville.overmap().regions:
        region.chance = 0  # nothing happens on the way
    hellsville = next(p for p in doomsville.overmap().places if p.name == "Hellsville")
    gone = doomsville.overmap_travel(hellsville.x * 1000, hellsville.y * 1000, "Hellsville")
    assert gone is not None and gone.arrived and not gone.stop
    assert 1.5 * 3600 < gone.seconds < 3.5 * 3600
    state = doomsville.area_state()
    me = state.spots[doomsville.held_name()]
    assert doomsville.area(me.area).name == "Hellsville"
    assert "The creek crossing" in state.overmap.known


def test_walking_off_the_farm_makes_the_next_cell_and_comes_in_at_its_edge(doomsville):
    beyond = doomsville.overmap_edge("west")
    assert beyond is not None
    gone = doomsville.overmap_cross("west")
    assert gone is not None and gone.arrived
    state = doomsville.area_state()
    me = state.spots[doomsville.held_name()]
    assert me.area.startswith("cell:")
    area = doomsville.area(me.area)
    assert me.x == area.width - 1 or me.x >= area.width - 4  # in at the east edge
    assert me.area.removeprefix("cell:") in state.overmap.explored
    assert me.area in doomsville.world_art.cells[me.area.removeprefix("cell:")].places[0].id
    # Kept: the same cell is the same area when come back to.
    assert doomsville.cell_area_id(cell_of_place(me.area)) == me.area


def test_a_made_cells_buildings_get_insides_from_the_kit(doomsville):
    om = doomsville.overmap()
    stillwater = next(p for p in om.places if p.name == "Stillwater")
    cell = om_rules.cell_of(om, stillwater.x * 1000, stillwater.y * 1000)
    area_id = doomsville.ensure_cell(cell)
    world = doomsville.world()
    assert world.places[area_id].name == "Stillwater"
    cabins = [p for p in world.places.values() if p.inside == area_id and p.kind == "building"]
    assert cabins and all(doomsville.world_art.layout_shown(c.id, "") for c in cabins)
    area = doomsville.area(area_id)
    assert any(e.place_id in {c.id for c in cabins} for e in area.entrances)


def test_a_place_made_later_gets_its_people_and_dead_from_the_design(doomsville):
    om = doomsville.overmap()
    stillwater = next(p for p in om.places if p.name == "Stillwater")
    gone = doomsville.overmap_travel(stillwater.x * 1000, stillwater.y * 1000, "Stillwater")
    assert gone is not None
    doomsville.ensure_cell(om_rules.cell_of(om, stillwater.x * 1000, stillwater.y * 1000))
    doomsville.visual_lay_out()
    state = doomsville.area_state()
    assert "Agnes Moore" in state.spots and "Tobias Grey" in state.spots
    carrick = next(p for p in om.places if p.name == "Carrick")
    area_id = doomsville.ensure_cell(om_rules.cell_of(om, carrick.x * 1000, carrick.y * 1000))
    doomsville.visual_lay_out()
    state = doomsville.area_state()
    assert "Carrick" in state.laid_places
    assert sum(1 for c in state.creatures.values() if c.area == area_id) >= 5


def test_eating_and_sleeping_keep_the_needs_and_going_without_shows(doomsville):
    state = doomsville.area_state()
    state.seconds += 30 * 3600
    doomsville._set_live(state)
    assert {"hungry", "thirsty", "weary"} <= set(doomsville.visual_needs())
    assert doomsville.needs_penalty() == 3
    state = doomsville.area_state()
    from sealedlore.engine.session_visual import ITEM_PREFIX

    state.things[ITEM_PREFIX + "beans"] = ThingRecord(
        state="taken", name="a tin of beans", by=doomsville.held_name(), count=2, kind="food"
    )
    doomsville._set_live(state)
    doomsville.visual_use(ITEM_PREFIX + "beans", "eat")
    assert "hungry" not in doomsville.visual_needs()


def test_the_keeper_brings_in_a_stranger_who_can_be_talked_to(doomsville):
    from sealedlore.engine.keeper import Op

    lines = doomsville.apply_script(
        [
            Op(
                op="stranger",
                args={
                    "who": "Mara Quist",
                    "card": "A woman walking east, alone, harmless.",
                    "attitude": "tired, wary, kind",
                    "where": "nearby",
                },
            ),
            Op(op="tell_of", args={"where": "Stillwater"}),
        ]
    )
    state = doomsville.area_state()
    assert "Mara Quist" in state.spots and doomsville.has_card("Mara Quist")
    assert doomsville.visual_sheet("Mara Quist").personality == "tired, wary, kind"
    assert "Mara Quist" in doomsville.visual_people()
    assert "Stillwater" in state.overmap.known
    assert any("Stillwater" in line for line in lines)


def test_a_journey_stopped_on_the_way_asks_the_keeper_with_the_region(doomsville):
    om = doomsville.overmap()
    for region in om.regions:
        region.chance = 100
    carrick = next(p for p in om.places if p.name == "Carrick")
    gone = doomsville.overmap_travel(carrick.x * 1000, carrick.y * 1000, "Carrick")
    assert gone is not None and gone.stop is not None and not gone.arrived
    assert "something" in gone.brief and "on the way from The farm to Carrick" in gone.brief
    state = doomsville.area_state()
    me = state.spots[doomsville.held_name()]
    assert doomsville.area(me.area) is not None
    messages = doomsville._keeper_messages("journey", gone.brief)
    asked = messages[-1].parts[0].text
    assert gone.brief in asked and "The program has decided that something happens" in asked


def _working_truck(session) -> str:
    """The farm's pickup made to work, as the keeper would."""
    from sealedlore.engine.visual_rules import drivable

    state = session.area_state()
    world = session.world()
    barn = next(p for p in world.places.values() if p.name == "The barn")
    area = session.area(barn.id)
    truck = next(p for p in area.placements if "truck" in p.name.lower())
    record = state.things.setdefault(truck.id, ThingRecord(state="seen", name=truck.name))
    record.kind, record.uses, record.tags = "vehicle", ["drive"], ["works", "fuelled"]
    me = state.spots[session.held_name()]
    x, y = truck.x, truck.y + truck.h
    state.spots[session.held_name()] = me.model_copy(update={"area": barn.id, "x": x, "y": y})
    session._set_live(state)
    assert drivable(record)
    return truck.id


def test_a_working_vehicle_drives_the_road_faster_and_comes_along(doomsville):
    for region in doomsville.overmap().regions:
        region.chance = 0
    truck = _working_truck(doomsville)
    assert [v[0] for v in doomsville.overmap_vehicles()] == [truck]
    hellsville = next(p for p in doomsville.overmap().places if p.name == "Hellsville")
    walking = doomsville.overmap_estimate(hellsville.x * 1000, hellsville.y * 1000)
    driving = doomsville.overmap_estimate(hellsville.x * 1000, hellsville.y * 1000, vehicle=truck)
    assert driving.walking * 4 < walking.walking
    gone = doomsville.overmap_travel(
        hellsville.x * 1000, hellsville.y * 1000, "Hellsville", vehicle=truck
    )
    assert gone.arrived and gone.lines[0].startswith("You drive the")
    state = doomsville.area_state()
    me = state.spots[doomsville.held_name()]
    record = state.things[truck]
    assert record.state == "dropped" and record.spot.area == me.area
    assert 100 < record.fuel_km < 150


def test_a_dry_tank_ends_the_drive_where_it_runs_out(doomsville):
    for region in doomsville.overmap().regions:
        region.chance = 0
    truck = _working_truck(doomsville)
    state = doomsville.area_state()
    state.things[truck].fuel_km = 3.0
    doomsville._set_live(state)
    stillwater = next(p for p in doomsville.overmap().places if p.name == "Stillwater")
    gone = doomsville.overmap_travel(
        stillwater.x * 1000, stillwater.y * 1000, "Stillwater", vehicle=truck
    )
    assert gone is not None and not gone.arrived
    assert "out of fuel" in gone.lines[-1] or any("out of fuel" in ln for ln in gone.lines)
    assert doomsville.area_state().things[truck].fuel_km == 0


def test_a_made_cell_is_painted_in_the_background_and_shown(doomsville, monkeypatch):
    from types import SimpleNamespace

    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication

    import sealedlore.engine.session_paint as paint
    from sealedlore.gui.area_render import TileSource
    from sealedlore.gui.backdrop_render import source_jpeg, trim_painting
    from sealedlore.gui.image_codec import encoded

    QApplication.instance() or QApplication([])
    sent = []

    class Painter:
        def __init__(self, base_url="", api_key="", **kw):
            self.api = kw.get("api", "nanogpt")

        def generate(self, payload):
            sent.append(payload)
            image = QImage(2048, 2048, QImage.Format_RGB32)
            image.fill(QColor("#ddeeff"))
            return SimpleNamespace(images=[encoded(image, "JPEG", 80)], cost=0.014)

        def cancel(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(paint, "ImageClient", Painter)
    monkeypatch.setattr(
        type(doomsville.config),
        "images",
        lambda self: SimpleNamespace(base_url="https://img.example", api_key="k", api="nanogpt"),
    )
    gone = doomsville.overmap_cross("west")
    state = doomsville.area_state()
    area = doomsville.area(state.spots[doomsville.held_name()].area)
    assert gone is not None and doomsville.paint_wanted(area)
    source = TileSource(*doomsville.tile_set(), cut_dir=None)
    done = []
    doomsville.on_painted = done.append
    assert doomsville.start_paint(
        area, lambda side: source_jpeg(area, source, side), lambda d: trim_painting(d, area)
    )
    doomsville._paint_thread.join(30)
    assert done == [area.id] and sent
    assert sent[0]["model"].endswith("edit-image") and sent[0]["imageDataUrls"]
    picture = doomsville.backdrop_of(area)
    assert picture is not None and doomsville.pictures.read(picture.file)
    assert not doomsville.paint_wanted(area)  # painted once, kept


def test_out_in_a_made_cell_the_plot_has_the_player_elsewhere(doomsville):
    for region in doomsville.overmap().regions:
        region.chance = 0
    hellsville = next(p for p in doomsville.overmap().places if p.name == "Hellsville")
    doomsville.overmap_travel(hellsville.x * 1000, hellsville.y * 1000, "Hellsville")
    doomsville.visual_plot_check()  # as the window does after every move
    assert doomsville.visual_chronicle().place == "Hellsville"
    brannock = next(p for p in doomsville.overmap().places if p.name == "Brannock")
    doomsville.overmap_travel(brannock.x * 1000, brannock.y * 1000, "Brannock")
    doomsville.visual_plot_check()
    chronicle = doomsville.visual_chronicle()
    assert chronicle.place is None and "Hellsville" in chronicle.visited
    assert "Brannock:" in doomsville.visual_state_text()


def test_the_game_window_offers_the_map_and_walks_on_into_cells(doomsville, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication, QDialog

    import sealedlore.gui.visual_window as vw
    from sealedlore.gui.overmap_view import OvermapDialog
    from sealedlore.models.config import Config

    QApplication.instance() or QApplication([])
    for region in doomsville.overmap().regions:
        region.chance = 0
    opened = []

    def shown(self):
        opened.append(self)
        self.chosen = None
        return QDialog.Rejected

    monkeypatch.setattr(OvermapDialog, "exec", shown)
    window = vw.VisualWindow(doomsville, Config(visual_paint=False), tmp_path)
    try:
        assert window.area_view.travel_button.text() == "Map…"
        window.area_view.travel_button.click()
        assert len(opened) == 1 and opened[0].places.count() >= 2
        window._edge_menu("west")
        window._edge_go("west", "area")
        window._fading = None
        state = doomsville.area_state()
        assert state.spots[doomsville.held_name()].area.startswith("cell:")
    finally:
        window.close()


# --- the playtest's fixes (Oct 27 2026) ---------------------------------------------------


def test_setting_out_an_hour_before_first_light_does_not_wait():
    """Live, a journey begun at 07:01 "waited for first light" (the walking
    day starts at 07:30)."""
    om = small_world()
    om.regions[0].chance = om.regions[1].chance = 0
    legs = om_rules.route(om, (1000, 6000), (10_000, 6000))
    traveller = _traveller(om, start_minute=7 * 60 + 1)
    done = om_rules.journey(legs, traveller, random.Random(1), place_name="Eastgate")
    assert done.arrived and not any("first light" in line for line in done.lines)
    assert done.seconds <= sum(leg.seconds for leg in legs) + 60


def test_a_journey_comes_in_on_the_road_where_it_crosses_the_edge(doomsville):
    """Live, a walk to Brannock landed in the cell's far corner, 30 tiles
    from the road and the street: the arrival point's coordinates were
    clamped one by one."""
    for region in doomsville.overmap().regions:
        region.chance = 0
    brannock = next(p for p in doomsville.overmap().places if p.name == "Brannock")
    gone = doomsville.overmap_travel(brannock.x * 1000, brannock.y * 1000, "Brannock")
    assert gone is not None and gone.arrived
    state = doomsville.area_state()
    me = state.spots[doomsville.held_name()]
    area = doomsville.area(me.area)
    assert area.name == "Brannock"
    assert area.rows[me.y][me.x] in "=b", (me.x, me.y)
    assert not (me.x < 3 and me.y > area.height - 4)


def test_a_clock_operation_reads_its_minutes_as_a_number_or_in_words():
    """Live, "Night passes. Morning, day 7" moved the clock not at all."""
    from sealedlore.engine.keeper import Op

    assert Op(op="clock", args={"minutes": "90"}).minutes() == 90
    assert Op(op="clock", args={"minutes": "two hours"}).minutes() == 120
    assert Op(op="clock", args={"minutes": "until morning"}).minutes(22 * 60) == 9 * 60
    assert Op(op="clock", args={"minutes": "overnight"}).minutes(7 * 60) == 24 * 60
    assert Op(op="clock", args={"minutes": "a day"}).minutes() == 24 * 60
    assert Op(op="clock", args={"minutes": "soon"}).minutes() == 0
    assert Op(op="clock", args={}).minutes() == 0


def test_a_broken_leg_slows_a_journey_and_the_road_mends_nothing(doomsville):
    from sealedlore.models.visual import Injury

    for region in doomsville.overmap().regions:
        region.chance = 0
    hellsville = next(p for p in doomsville.overmap().places if p.name == "Hellsville")
    whole = doomsville.overmap_estimate(hellsville.x * 1000, hellsville.y * 1000)
    state = doomsville.area_state()
    state.injuries.append(
        Injury(kind="broken leg", serious=True, at=0, treated=0, words="a broken leg")
    )
    doomsville._set_live(state)
    slow = doomsville.overmap_estimate(hellsville.x * 1000, hellsville.y * 1000)
    assert slow.walking > whole.walking * 1.8
    gone = doomsville.overmap_travel(hellsville.x * 1000, hellsville.y * 1000, "Hellsville")
    assert gone is not None and gone.arrived
    leg = doomsville.area_state().injuries[0]
    assert 0 < leg.rested < gone.seconds  # up and about: rest counts half
