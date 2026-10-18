"""Rules from the three concurrent playtests of Oct 18 2026 (a Doomsville
without a map, Brasshaven on its map, a premise story on a grown map)."""

from __future__ import annotations

import time

from sealedlore.engine.area import Area, thing_tile
from sealedlore.engine.regions import RegionDesign, build_region
from sealedlore.engine.world import _elsewhere, _left_outside, find_place
from sealedlore.engine.world_layout import WIDE_AROUND, place_new
from sealedlore.models.world import WorldDelta, WorldMap, WorldPlace


def _town() -> tuple[WorldMap, WorldPlace, WorldPlace]:
    town = WorldPlace(name="Marrowgate", kind="settlement", x=0.0, y=0.0, size=1500.0)
    region = WorldPlace(name="Marrowgate", kind="region", x=0.0, y=0.0, size=400.0)
    world = WorldMap(places={region.id: region, town.id: town})
    return world, town, region


def test_a_name_shared_by_a_region_and_a_town_means_the_town():
    world, town, region = _town()
    assert list(world.places)[0] == region.id  # the region is listed first
    assert find_place(world, "Marrowgate") == town.id
    assert find_place(world, "the Marrowgate") == town.id


def test_from_a_room_of_the_building_to_the_building_is_not_from_outside():
    shop = WorldPlace(name="The Gilded Gear", kind="building", x=0.0, y=0.0, size=30.0)
    back = WorldPlace(name="The back room", kind="room", inside=shop.id, x=0.0, y=0.0, size=5.0)
    world = WorldMap(places={shop.id: shop, back.id: back}, where={"Silas": back.id})
    quote = "Silas came through the curtain with the automaton over his shoulder"
    assert not _left_outside(world, WorldDelta(), "Silas", shop.id, quote)
    # From another place, the quote not showing them going in: outside still.
    world.where["Silas"] = "elsewhere"
    assert _left_outside(world, WorldDelta(), "Silas", shop.id, quote)


def test_a_house_in_a_town_that_reaches_the_region_is_not_elsewhere():
    world, town, _region = _town()
    hall = WorldPlace(
        name="Reeve's hall", kind="building", inside=town.id, x=-43.0, y=68.0, size=40.0
    )
    world.places[hall.id] = hall
    # Ines stands in the grid cell west of the hall's; the town spans both.
    region = (-600.0, 200.0, 400.0)
    entry = {"who": "Ines Vael", "place": "Reeve's hall", "quote": "walked up through the town"}
    source = "She walked up through the town and knocked."
    assert not _elsewhere(world, WorldDelta(), hall.id, region, source, entry)
    hut = WorldPlace(name="the hut", kind="building", x=900.0, y=900.0, size=10.0)
    world.places[hut.id] = hut
    assert _elsewhere(world, WorldDelta(), hut.id, region, source, {**entry, "place": "the hut"})


def test_a_wide_new_region_with_no_way_said_lies_round_the_story_s_place():
    world, town, _region = _town()
    wharf = WorldPlace(name="The wharf", kind="area", inside=town.id, x=0.0, y=0.0, size=120.0)
    world.places[wharf.id] = wharf
    delta = WorldPlace(name="the delta", kind="region")
    place_new(world, [delta], [], wharf.id, (10.0, -20.0))
    assert delta.size >= WIDE_AROUND * 1500.0
    assert (delta.x, delta.y) == (10.0, -20.0)
    shed = WorldPlace(name="the shed", kind="building")
    place_new(world, [shed], [], wharf.id, (10.0, -20.0))
    assert shed.x is not None and abs(shed.x) < 2000  # beside, as before


def test_a_designed_region_named_for_the_story_s_place_is_its_outskirts():
    town = WorldPlace(name="Marrowgate", kind="settlement", x=0.0, y=0.0, size=1500.0)
    design = RegionDesign(name="Marrowgate", what="the town", ground="grass")
    region, _patch = build_region(
        design,
        key="0:0",
        rect=(-200.0, 200.0, 400.0),
        holder_id=None,
        labels_from=101,
        story=[town],
    )
    assert region.places[0].name == "the outskirts of Marrowgate"
    other = RegionDesign(name="the Cull Marsh", what="marsh", ground="grass")
    region, _patch = build_region(
        other, key="1:0", rect=(200.0, 200.0, 400.0), holder_id=None, labels_from=101, story=[town]
    )
    assert region.places[0].name == "the Cull Marsh"


def test_laying_a_thing_on_a_wide_window_looks_only_nearby():
    rows = ["." * 160 for _ in range(160)]
    area = Area(
        place_id="w",
        name="w",
        indoor=False,
        look="grass",
        rows=rows,
        tile_m=2.5,
        objects={},
        labels={},
    )
    t0 = time.perf_counter()
    taken: set[tuple[int, int]] = set()
    for i in range(40):
        x, y = thing_tile(area, (80, 80), taken)
        assert abs(x - 80) <= 24 and abs(y - 80) <= 24
        taken.add((x, y))
    assert time.perf_counter() - t0 < 2.0
