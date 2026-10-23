"""The board (engine/board.py): the areas laid out by direction, not to
scale, for the map's Region and World levels."""

from __future__ import annotations

from sealedlore.engine.board import CELL_M, NODE_M, board_cells, board_world, is_node, ways
from sealedlore.engine.world_layout import position
from sealedlore.models.world import WorldLink, WorldMap, WorldPlace


def _world() -> WorldMap:
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0, y=0, size=400)
    house = WorldPlace(id="house", name="The farmhouse", kind="building", inside="farm", x=0, y=50)
    road = WorldPlace(id="road", name="The north road", kind="route", x=0, y=4000)
    town = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0, y=8000, size=1500)
    diner = WorldPlace(id="diner", name="The diner", kind="building", inside="town", x=-180, y=7750)
    creek = WorldPlace(id="creek", name="The creek crossing", kind="site", x=150, y=2600, size=100)
    hills = WorldPlace(id="hills", name="The hills", kind="area", x=4800, y=8600, size=800)
    caves = WorldPlace(id="caves", name="The Hidden Caves", kind="site", x=5200, y=9100, size=200)
    world = WorldMap(
        places={p.id: p for p in (farm, house, road, town, diner, creek, hills, caves)},
        links=[
            WorldLink(a="farm", b="road", direction="north"),
            WorldLink(a="road", b="farm", direction="south"),
            WorldLink(a="road", b="town", direction="north"),
            WorldLink(a="town", b="road", direction="south"),
            WorldLink(a="creek", b="road", direction="west, beside it"),
            WorldLink(a="town", b="hills", direction="east, about three miles"),
            WorldLink(a="hills", b="caves", direction="east, up the slope"),
        ],
        start="farm",
    )
    return world


def test_the_nodes_are_the_areas_and_the_routes_join_them():
    world = _world()
    assert {p for p in world.places if is_node(world, world.places[p])} == {
        "farm",
        "town",
        "creek",
        "hills",
        "caves",
    }
    joined = ways(world)
    assert ("town", 0.0, "road") in joined["farm"]  # north, along the road
    assert ("farm", 180.0, "road") in joined["town"]
    assert ("hills", 90.0, None) in joined["town"]
    assert ("caves", 90.0, None) in joined["hills"]


def test_each_node_takes_a_cell_a_step_in_its_way_s_direction():
    cells = board_cells(_world())
    assert cells["farm"] == (0, 0)
    assert cells["town"] == (0, -1)  # north of the farm
    assert cells["hills"] == (1, -1)  # east of the town
    assert cells["caves"] == (2, -1)  # east again
    # The creek, west of the road, lands by its bearing from the road's
    # nodes (north-east of the farm on the map), off a cell already taken.
    assert cells["creek"] not in {cells["farm"], cells["town"], cells["hills"], cells["caves"]}


def test_the_board_world_draws_every_node_the_same_width_with_its_insides_scaled():
    world = _world()
    shown = board_world(world)
    assert position(shown, "farm") == (0.0, 0.0)
    assert position(shown, "town") == (0.0, CELL_M)
    assert shown.places["farm"].size == shown.places["town"].size == NODE_M
    diner = shown.places["diner"]
    town = shown.places["town"]
    assert abs(diner.x - town.x) < NODE_M / 2 and abs(diner.y - town.y) < NODE_M / 2
    assert diner.size < 40  # scaled with the town, 1500 m into NODE_M
    # The road lies among the cells it joins (the farm, the town, the creek
    # beside it); the story's own map is untouched.
    road = position(shown, "road")
    assert (
        road is not None and abs(road[0] - CELL_M / 3) < 1e-6 and abs(road[1] - CELL_M / 3) < 1e-6
    )
    assert position(world, "town") == (0.0, 8000.0)
