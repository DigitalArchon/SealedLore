"""Where places sit on the map (engine/world_layout.py).

A place is placed once, from what the story said, and never moved: the map
grows past its edge by adding places beyond it, and what was placed stays.
"""

from __future__ import annotations

import math

import pytest

from sealedlore.engine.world import applied, place_delta, world_at
from sealedlore.engine.world_layout import bearing, extent, metres, size_of
from sealedlore.models.node import Node, NodeMeta
from sealedlore.models.world import WorldDelta, WorldLink, WorldMap, WorldPlace


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("five miles", 8_046.72),
        ("5 miles", 8_046.72),
        ("twenty-one miles", 33_796.2),
        ("half a mile", 804.67),
        ("a mile and a half", 2_414.02),
        ("2.5 km", 2_500.0),
        ("three klicks", 3_000.0),
        ("thirty yards", 27.43),
        ("two hours on foot", 10_000.0),
        ("an hour's drive", 50_000.0),
        ("a day's ride", 96_000.0),
        ("twenty minutes' walk", 1_666.67),
        ("a long way", None),
        (None, None),
    ],
)
def test_distances_are_read_in_the_texts_own_words(words, expected):
    got = metres(words)
    assert got is None if expected is None else got == pytest.approx(expected, rel=1e-3)


def test_bearings_come_only_from_words_that_name_one():
    assert bearing("north") == 0
    assert bearing("south-east") == 135
    assert bearing("due west, past the ridge") == 270
    assert bearing("around back") is None
    assert bearing(None) is None


def place(name: str, kind: str | None = None, inside: str | None = None) -> WorldPlace:
    return WorldPlace(id=name.lower().replace(" ", "-"), name=name, kind=kind, inside=inside)


def grow(world: WorldMap, delta: WorldDelta, held: str | None = None) -> WorldMap:
    place_delta(world, delta, held)
    return applied(world, delta)


def at(world: WorldMap, name: str) -> tuple[float, float]:
    p = next(p for p in world.places.values() if p.name == name)
    return p.x, p.y


def test_the_first_place_is_the_origin_and_a_link_places_the_next():
    farm, town = place("Farmhouse", "building"), place("Hellsville", "settlement")
    world = grow(
        WorldMap(),
        WorldDelta(
            places=[farm, town],
            links=[WorldLink(a=farm.id, b=town.id, direction="north", distance="five miles")],
        ),
    )
    assert at(world, "Farmhouse") == (0.0, 0.0)
    x, y = at(world, "Hellsville")
    assert x == pytest.approx(0, abs=1e-6) and y == pytest.approx(8_046.72, rel=1e-3)


def test_a_link_from_the_new_place_points_back_the_other_way():
    town = place("Hellsville", "settlement")
    world = grow(WorldMap(), WorldDelta(places=[town]))
    farm = place("Farmhouse", "building")
    world = grow(
        world,
        WorldDelta(
            places=[farm],
            links=[WorldLink(a=town.id, b=farm.id, direction="south", distance="five miles")],
        ),
    )
    x, y = at(world, "Farmhouse")
    assert y == pytest.approx(-8_046.72, rel=1e-3) and x == pytest.approx(0, abs=1e-6)


def test_a_place_inside_another_is_drawn_within_it():
    town = place("Hellsville", "settlement")
    world = grow(WorldMap(), WorldDelta(places=[town]))
    diner = place("Diner", "building", inside=town.id)
    clinic = place("Clinic", "building", inside=town.id)
    world = grow(world, WorldDelta(places=[diner, clinic]))
    for name in ("Diner", "Clinic"):
        x, y = at(world, name)
        assert math.hypot(x, y) + 20 <= size_of(world.places[town.id]) / 2
    assert at(world, "Diner") != at(world, "Clinic")


def test_a_room_bigger_than_its_building_is_kept_inside_it():
    house = place("House", "building")
    world = grow(WorldMap(), WorldDelta(places=[house]))
    yard = place("Yard", "area", inside=house.id)
    world = grow(world, WorldDelta(places=[yard]))
    assert size_of(world.places[yard.id]) <= size_of(world.places[house.id]) * 0.45


def test_places_with_nothing_to_go_on_never_overlap():
    first = place("Camp", "site")
    world = grow(WorldMap(), WorldDelta(places=[first], where={"Jane": first.id}))
    names = ["Ruin", "Ridge", "Ford", "Mill", "Well", "Tower"]
    world = grow(world, WorldDelta(places=[place(n, "site") for n in names]), held="Jane")
    placed = list(world.places.values())
    for i, a in enumerate(placed):
        for b in placed[i + 1 :]:
            assert math.hypot(a.x - b.x, a.y - b.y) >= (size_of(a) + size_of(b)) / 2 - 1e-6


def test_an_established_place_never_moves_and_a_disagreement_is_noted():
    farm, town = place("Farmhouse", "building"), place("Hellsville", "settlement")
    world = grow(
        WorldMap(),
        WorldDelta(
            places=[farm, town],
            links=[WorldLink(a=farm.id, b=town.id, direction="north", distance="five miles")],
        ),
    )
    before = at(world, "Hellsville")
    # A later passage says forty miles: the map keeps where the town is.
    later = WorldDelta(
        links=[WorldLink(a=farm.id, b=town.id, direction="north", distance="forty miles")]
    )
    world = grow(world, later)
    assert at(world, "Hellsville") == before
    assert any("about 8.0 km apart" in note and "forty miles" in note for note in later.conflicts)


def test_walking_off_the_edge_grows_the_map_beyond_it():
    farm = place("Farmhouse", "building")
    world = grow(WorldMap(), WorldDelta(places=[farm], where={"Jane": farm.id}))
    west, *_ = extent(world)
    hills = place("The western hills", "region")
    world = grow(
        world,
        WorldDelta(
            places=[hills],
            links=[WorldLink(a=farm.id, b=hills.id, direction="west", distance="a day's walk")],
            where={"Jane": hills.id},
        ),
        held="Jane",
    )
    assert at(world, "The western hills")[0] < west
    assert at(world, "Farmhouse") == (0.0, 0.0)
    assert extent(world)[0] < west


def test_placing_is_deterministic():
    def build() -> WorldMap:
        camp = place("Camp", "site")
        world = grow(WorldMap(), WorldDelta(places=[camp], where={"Jane": camp.id}))
        return grow(
            world, WorldDelta(places=[place(n, "site") for n in ("A", "B", "C")]), held="Jane"
        )

    assert build().model_dump() == build().model_dump()


def test_a_place_stored_without_a_position_is_placed_when_the_map_is_folded():
    node = Node(
        kind="assistant",
        speaker_id="__narrator__",
        content="x",
        meta=NodeMeta(world=WorldDelta(places=[place("Camp", "site")])),
    )
    world = world_at([node])
    assert at(world, "Camp") == (0.0, 0.0)
    assert world_at([node]).model_dump() == world.model_dump()


def test_a_room_inside_a_vehicle_moves_with_it():
    from sealedlore.engine.world_layout import position

    station = place("Station", "settlement")
    world = grow(WorldMap(), WorldDelta(places=[station]))
    ship = place("Sable Crest", "vehicle", inside=station.id)
    bridge = place("Bridge", "room", inside=ship.id)
    world = grow(world, WorldDelta(places=[ship, bridge]))
    assert world.places[bridge.id].x is None
    assert position(world, bridge.id) == position(world, station.id)


def test_a_place_in_a_wide_parent_is_put_by_the_author_s_character_not_off_its_far_side():
    # The Silverwood, live (Oct 16 2026): a rift "above the flames" across
    # the river from the village, inside a forest 16 km across. The link to
    # its own forest was taken as a bearing, and it landed 6.6 km off,
    # outside the forest.
    village = place("Fernhollow", "settlement")
    village.x, village.y, village.size = 0.0, 0.0, 800.0
    forest = place("The Silverwood", "area")
    forest.x, forest.y, forest.size = 2000.0, 8000.0, 16000.0
    world = WorldMap(places={village.id: village, forest.id: forest}, where={"Elowen": village.id})
    rift = place("The rift", "site", inside=forest.id)
    rift.size = 300.0
    world = grow(
        world,
        WorldDelta(places=[rift], links=[WorldLink(a=rift.id, b=forest.id, direction="above")]),
        held="Elowen",
    )
    x, y = at(world, "The rift")
    # In the forest's drawn rectangle (16 km by 9.6 km round its spot), at
    # the edge nearest the village: the plan's south edge is 3.2 km off.
    assert abs(x - 2000) <= 8000 - 150 and abs(y - 8000) <= 4800 - 150
    assert abs(x) <= 200 and y <= 3400
