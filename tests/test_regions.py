"""The world designer (engine/regions.py, engine/session_regions.py): the
author (Oct 18 2026) wants the world built as the player goes, "like a
procedurally generated game, but ... the AI acts as a real designer": regions
round the player designed ahead of them, at walking scale, with places,
things and people, joined into one map that grows as it is explored."""

from __future__ import annotations

import json
import math

import pytest

from sealedlore.engine.regions import (
    DESIGN_GRID,
    LABEL_FROM,
    REGION_SIDE,
    REGION_TILES,
    WALKABLE_LAND,
    build_region,
    cell_key,
    cell_of,
    cell_rect,
    designed_delta,
    edge_lines,
    origin_of,
    parse_region,
    waves,
)
from sealedlore.engine.world import world_at
from sealedlore.engine.world_art import window_rect
from sealedlore.models.world import WorldArt, WorldMap, WorldPlace
from sealedlore.providers.mock import MockChatProvider
from tests.test_open_country import open_session, turn

DESIGN = {
    "name": "The Kessler fields",
    "what": "Dead farmland under snow, a fence line and the Kessler barn.",
    "ground": "snow",
    "features": [
        {"kind": "area", "fill": "trees", "x": 0, "y": 0, "w": 20, "h": 12},
        {"kind": "area", "fill": "water", "x": 0, "y": 40, "w": 80, "h": 3},
        {"kind": "road", "points": [[40, 0], [40, 79]], "width": 2},
        {"kind": "fence", "points": [[50, 20], [70, 20]]},
        {"kind": "building", "x": 5, "y": 5, "w": 4, "h": 4},
    ],
    "places": [
        {"name": "the Kessler barn", "kind": "building", "what": "A sagging red barn.",
         "x": 55, "y": 25, "w": 6, "h": 5},
        {"name": "a burned-out pickup", "kind": "landmark", "what": "Rusted through.",
         "x": 20, "y": 60, "w": 2, "h": 1},
    ],
    "things": [{"name": "a rusted fuel can", "x": 57, "y": 30},
               {"name": "a bundle of fence wire", "x": 10, "y": 70}],
    "people": [{"who": "a zombie in a torn parka", "doing": "stumbling along the fence",
                "x": 60, "y": 22}],
}  # fmt: skip


def designed(
    world: WorldMap | None = None, art: WorldArt | None = None, cell=(0, 0), holder_id=None
):
    world = world or WorldMap()
    art = art or WorldArt()
    origin = origin_of(world, art)
    art.region_origin = list(origin)
    region, patch = build_region(
        parse_region(json.dumps(DESIGN)),
        key=cell_key(*cell),
        rect=cell_rect(origin, *cell),
        holder_id=holder_id,
        labels_from=LABEL_FROM,
    )
    art.regions[region.key] = region
    art.terrain.append(patch)
    return world, art, region, patch


def test_the_grid_is_laid_round_where_the_story_starts_and_designed_in_waves():
    world = WorldMap(
        places={"farm": WorldPlace(id="farm", name="The farm", kind="site", x=100.0, y=-50.0)},
        start="farm",
    )
    origin = origin_of(world, WorldArt())
    assert origin == (100.0 - REGION_SIDE / 2, -50.0 + REGION_SIDE / 2)
    assert cell_of(origin, 100.0, -50.0) == (0, 0)  # the start is the middle of its region
    assert cell_of(origin, 100.0 + REGION_SIDE, -50.0) == (1, 0)
    assert cell_of(origin, 100.0, -50.0 - REGION_SIDE) == (0, 1)  # rows run south
    west, north, side = cell_rect(origin, 1, 0)
    assert (west, north, side) == (origin[0] + REGION_SIDE, origin[1], REGION_SIDE)
    first, sides, corners = waves(origin, (100.0, -50.0))
    assert first == [(0, 0)]
    assert sorted(sides) == [(-1, 0), (0, -1), (0, 1), (1, 0)]
    assert len(corners) == 4 and all(abs(i) == abs(j) == 1 for i, j in corners)


def test_a_reply_is_read_and_held_to_the_grid():
    design = parse_region(json.dumps(DESIGN))
    assert design.name == "The Kessler fields" and design.ground == "snow"
    assert [f["kind"] for f in design.features] == ["area", "area", "road", "fence"]  # no building
    assert [p["kind"] for p in design.places] == ["building", "area"]
    odd = dict(DESIGN, ground="lava", places=[{"name": "x", "kind": "castle", "x": 900, "y": -4}])
    read = parse_region(json.dumps(odd))
    assert read.ground == "dirt" and read.places[0]["kind"] == "area"
    assert read.places[0]["x"] == DESIGN_GRID - 1 and read.places[0]["y"] == 0
    with pytest.raises(ValueError):
        parse_region(json.dumps({"what": "no name"}))


def test_a_design_is_land_at_walking_scale_that_never_shuts_the_player_in():
    _world, _art, region, patch = designed()
    assert len(patch.rows) == REGION_TILES and len(patch.rows[0]) == REGION_TILES
    assert REGION_SIDE / REGION_TILES <= 3.0  # walking scale
    assert patch.designed and patch.ground == "snow"
    # The river across the whole region is bridged on the way to each edge.
    mid = REGION_TILES // 2
    reached = {(mid, mid)}
    todo = [(mid, mid)]
    while todo:
        x, y = todo.pop()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < REGION_TILES and 0 <= ny < REGION_TILES and (nx, ny) not in reached:
                if patch.rows[ny][nx] in WALKABLE_LAND:
                    reached.add((nx, ny))
                    todo.append((nx, ny))
    for edge in ((mid, 0), (mid, REGION_TILES - 1), (0, mid), (REGION_TILES - 1, mid)):
        assert edge in reached
    assert any("b" in row for row in patch.rows)  # a bridge over the river
    places = {p.name: p for p in region.places}
    assert places["The Kessler fields"].kind == "region"
    barn = places["the Kessler barn"]
    assert barn.kind == "building" and barn.inside == region.place_id
    assert region.west < barn.x < region.west + REGION_SIDE
    assert region.north - REGION_SIDE < barn.y < region.north
    can = next(t for t in region.things if "fuel" in t.name)
    wire = next(t for t in region.things if "wire" in t.name)
    assert can.at == barn.id and wire.at == region.place_id  # beside the barn, or in the open
    assert [p.label for p in region.people] == [f"Unknown {LABEL_FROM}"]


def test_the_regions_are_world_on_every_branch_and_under_fog_until_explored():
    world, art, region, _patch = designed()
    delta = designed_delta(art)
    folded = world_at([], None, designed=delta)
    assert region.place_id in folded.places and "the Kessler barn" in {
        p.name for p in folded.places.values()
    }
    label = region.people[0].label
    assert folded.where[label] == region.place_id
    assert folded.unknowns[label] == "a zombie in a torn parka"
    assert not set(folded.seen) & {p.id for p in region.places}
    art.explored.append(region.key)
    seen = world_at([], None, designed=designed_delta(art)).seen
    assert {p.id for p in region.places} <= set(seen)


def test_a_region_beside_one_tells_its_designer_what_crosses_the_edge():
    world, art, _region, _patch = designed()
    origin = (art.region_origin[0], art.region_origin[1])
    lines = edge_lines(art, origin, 0, 1)  # the region south of it
    assert "north: The Kessler fields" in lines
    assert "a road at x 40-41" in lines  # the road runs on south across the edge
    assert edge_lines(art, origin, 5, 5) == "- none designed yet"


def _with_region(session, holder_id=None):
    world = session.world()
    art = session.world_art
    cell = cell_of(origin_of(world, art), 800.0, 800.0)
    _w, _a, region, _patch = designed(world, art, cell=cell, holder_id=holder_id)
    session._area_cache = None
    session._designed_cache = None
    return region


def test_open_country_on_a_designed_region_is_its_land_with_its_places_things_and_people(
    tmp_path, story, cast
):
    session = open_session(tmp_path, story, cast)
    region = _with_region(session)
    world, art = session.world(), session.map_art_copy()
    west, north, side = window_rect(art, 800.0, 800.0)
    assert (west, north, side) == (region.west, region.north, region.side)
    area = session.area(f"window:{west:.0f}:{north:.0f}:{side:.0f}")
    assert area.tile_m == REGION_SIDE / REGION_TILES and area.place_id == region.place_id
    barn = next(p for p in world.places.values() if p.name == "the Kessler barn")
    # Boxed with a door; walked into once its inside is drawn, as any building.
    x0, y0, x1, y1 = area.buildings[barn.id]
    walls = [area.rows[y][x] for y in range(y0, y1 + 1) for x in (x0, x1)]
    walls += [area.rows[y][x] for x in range(x0, x1 + 1) for y in (y0, y1)]
    assert "+" in walls
    assert any("fence wire" in p.name for p in area.placements)
    label = region.people[0].label
    state = session.area_state()
    spot = state.spots[label]
    assert spot.area == area.id
    x, y = region.people[0].x, region.people[0].y
    tx = (x - west) / area.tile_m
    ty = (north - y) / area.tile_m
    assert math.hypot(spot.x - tx, spot.y - ty) <= 3  # where the designer put them


def test_the_storyteller_is_told_the_region_and_its_places_with_their_distances(
    tmp_path, story, cast
):
    session = open_session(tmp_path, story, cast, ["Snow.", "{}"])
    region = _with_region(session)
    session.story.world_base.where["Serrik Vaun"] = region.place_id
    session._designed_cache = None
    list(session.send(turn("I look along the fence.")))
    sent = session.provider.requests[0].messages[-1].text
    assert "The Kessler fields" in sent and "the Kessler barn" in sent
    assert " m " in sent[sent.find("the Kessler barn") - 80 : sent.find("the Kessler barn") + 80]


def test_the_storyteller_is_told_every_region_in_full_far_ones_too(tmp_path, story, cast):
    """Measured (Oct 19 2026, SealedLoreDocs design/map.md): told only the
    region it stood in and its neighbours by name, the storyteller's passages
    lost to every region in full with every judge on both models."""
    session = open_session(tmp_path, story, cast, ["Snow.", "{}"])
    region = _with_region(session)
    art = session.world_art
    origin = (art.region_origin[0], art.region_origin[1])
    i, j = cell_of(origin, 800.0, 800.0)
    far = {**DESIGN, "name": "The Hollow Reach", "what": "A drowned quarry under black ice.",
           "places": [], "things": [], "people": []}  # fmt: skip
    built, patch = build_region(
        parse_region(json.dumps(far)),
        key=cell_key(i + 3, j),
        rect=cell_rect(origin, i + 3, j),
        holder_id=None,
        labels_from=LABEL_FROM + 50,
    )
    art.regions[built.key] = built
    art.terrain.append(patch)
    session.story.world_base.where["Serrik Vaun"] = region.place_id
    session._designed_cache = None
    list(session.send(turn("I look along the fence.")))
    sent = session.provider.requests[0].messages[-1].text
    assert "The Hollow Reach" in sent and "drowned quarry" in sent


class _Designer(MockChatProvider):
    """A provider whose background clients answer every design with DESIGN."""

    made: list[MockChatProvider] = []

    def detached(self):
        client = MockChatProvider([json.dumps(DESIGN)] * 4)
        self.made.append(client)
        return client


def test_the_regions_round_the_player_are_designed_ahead_on_the_story_model(
    tmp_path, story, cast, monkeypatch
):
    session = open_session(tmp_path, story, cast, ["Snow.", "{}"])
    session.config.map_art = True
    list(session.send(turn("I set out across the erg.")))
    designer = _Designer([])
    monkeypatch.setattr(session, "provider", designer)
    assert session._start_region_design()
    session._region_thread.join(30)
    art = session.world_art
    assert len(art.regions) == 9
    origin = (art.region_origin[0], art.region_origin[1])
    here = cell_key(*cell_of(origin, *session.held_point()))
    assert here in art.regions and here in art.explored
    labels = [p.label for r in art.regions.values() for p in r.people]
    assert len(labels) == len(set(labels)) == 9  # one each, never the same
    sent = [c.requests[0] for c in designer.made if c.requests]
    assert sent and all(r.model == session.model for r in sent)  # the story model
    assert "The region to design" in sent[0].messages[-1].text
    assert not session._start_region_design()  # nothing left to design here


def test_nobody_is_designed_within_sight_of_the_player_nor_the_story_s_places_twice():
    """The live test (Oct 18 2026): the region John stood in was designed
    with a scavenger at the tree line beside him (simply there), and a
    second farmhouse beside the story's own."""
    world = WorldMap(start=None)
    origin = origin_of(world, WorldArt())
    rect = cell_rect(origin, 0, 0)
    west, north, _side = rect
    tile = REGION_SIDE / DESIGN_GRID
    # The zombie at the fence (60, 22) is 70-odd m from a player at (50, 20).
    player = (west + 50.5 * tile, north - 20.5 * tile)
    barn_at = (west + 58 * tile, north - 27.5 * tile)
    story = [WorldPlace(id="h", name="The Kessler Barn", kind="building", x=0.0, y=0.0, size=20.0)]
    region, _patch = build_region(
        parse_region(json.dumps(DESIGN)),
        key="0:0",
        rect=rect,
        holder_id=None,
        labels_from=LABEL_FROM,
        story=story,
        player=player,
    )
    assert region.people == []
    assert "the Kessler barn" not in {p.name for p in region.places}  # the story's, by name
    can = next(t for t in region.things if "fuel" in t.name)
    assert can.at == region.place_id  # its barn left out: lying in the open
    on_it = [WorldPlace(id="s", name="The old shed", kind="building", x=barn_at[0], y=barn_at[1])]
    again, _patch = build_region(
        parse_region(json.dumps(DESIGN)),
        key="0:0",
        rect=rect,
        holder_id=None,
        labels_from=LABEL_FROM,
        story=on_it,
    )
    assert "the Kessler barn" not in {p.name for p in again.places}  # where a story place stands


def test_a_region_is_told_the_others_names_and_not_their_places_as_the_story_s():
    from sealedlore.engine.regions import place_lines, taken_lines

    world, art, region, _patch = designed()
    folded = world_at([], None, designed=designed_delta(art))
    origin = (art.region_origin[0], art.region_origin[1])
    beside = cell_rect(origin, 1, 0)
    ids = {p.id for p in region.places}
    assert "Kessler barn" in place_lines(folded, beside)
    assert "Kessler barn" not in place_lines(folded, beside, designed=ids)
    taken = taken_lines(art)
    assert "The Kessler fields" in taken and "the Kessler barn" in taken and "fuel can" in taken


def test_the_designer_s_labels_never_collide_with_the_story_s_unknowns():
    """The review (Oct 20 2026): the designer numbered from its own labels
    (101…), the story from the folded map's count, which the designed labels
    raise; a story unknown made after the first wave took 104, and the next
    wave's designer gave 104 again: the designed person was then the story's
    unknown on every fold."""
    from sealedlore.engine.regions import designed_delta, next_label
    from sealedlore.engine.world import next_unknown
    from sealedlore.models.node import Node, NodeMeta
    from sealedlore.models.world import DesignedPerson, DesignedRegion, WorldDelta

    art = WorldArt()
    region = WorldPlace(
        id="r00", name="The back forty", kind="region", x=200.0, y=-200.0, size=400.0
    )
    art.regions["0:0"] = DesignedRegion(
        key="0:0",
        west=0,
        north=0,
        side=400,
        place_id="r00",
        places=[region],
        people=[
            DesignedPerson(
                label=f"Unknown {LABEL_FROM + i}", who=f"a scavenger {i}", x=50.0 * i, y=-50.0
            )
            for i in range(3)
        ],
    )
    world = world_at([], designed=designed_delta(art))
    label = next_unknown(world)
    assert label == f"Unknown {LABEL_FROM + 3}"
    # The story makes that unknown; the next wave's designer counts it.
    story = WorldDelta(unknowns={label: "the teenage girl"}, where={label: "r00"})
    node = Node(kind="assistant", speaker_id="narrator", content="x", meta=NodeMeta(world=story))
    later = world_at([node], designed=designed_delta(art))
    assert next_label(art) == LABEL_FROM + 3  # the designer's own count alone: the collision
    assert next_label(art, later) == LABEL_FROM + 4
    zombie = f"Unknown {next_label(art, later)}"
    art.regions["1:0"] = DesignedRegion(
        key="1:0",
        west=400,
        north=0,
        side=400,
        place_id="r10",
        places=[
            WorldPlace(id="r10", name="The ridge", kind="region", x=600.0, y=-200.0, size=400.0)
        ],
        people=[DesignedPerson(label=zombie, who="a wandering zombie", x=600.0, y=-300.0)],
    )
    after = world_at([node], designed=designed_delta(art))
    assert after.unknowns[label] == "the teenage girl" and after.where[label] == "r00"
    assert after.unknowns[zombie] == "a wandering zombie" and after.where[zombie] == "r10"
    # A story unknown named since still counts: its number is not given again.
    named = WorldDelta(named={label: "Ruth"})
    node2 = Node(kind="assistant", speaker_id="narrator", content="x", meta=NodeMeta(world=named))
    assert next_label(art, world_at([node, node2], designed=designed_delta(art))) == LABEL_FROM + 5


def test_someone_in_the_player_s_region_is_not_pulled_beside_them_as_company(tmp_path, story, cast):
    """The live test: the read put John "at the North Woodlot", a region,
    and two of its people were stood beside him as if in his company."""
    session = open_session(tmp_path, story, cast)
    region = _with_region(session)
    session.story.world_base.where["Serrik Vaun"] = region.place_id
    session._designed_cache = None
    label = region.people[0].label
    state = session.area_state()
    me, them = state.spots["Serrik Vaun"], state.spots[label]
    session._set_live(state)
    again = session.area_state().spots[label]
    assert (again.area, again.x, again.y) == (them.area, them.x, them.y)
    assert math.hypot(them.x - me.x, them.y - me.y) > 5


def test_every_edge_of_a_region_is_reached_after_its_buildings_are_stamped():
    """The live test (Oct 18 2026): the region's land kept a way from its
    middle to each edge, then the story's farmhouse was stamped in the
    middle and cut the road east."""
    from sealedlore.engine.area import CARVABLE, WALKABLE, edges_open

    rows = [
        "TTTTTTTTTT",
        "T........T",
        "T..####..T",
        "T..#  #..T",
        "T..####..T",
        "T........T",
        "TTTTTTTTTT",
    ]
    repairs: list[str] = []
    opened = edges_open(rows, repairs)
    assert len(repairs) == 4  # a way cut to each edge
    for edge in ([opened[0]], [opened[-1]], [[r[0] for r in opened]], [[r[-1] for r in opened]]):
        assert any(ch in WALKABLE for ch in edge[0])
    assert opened[2][3:7] == "####" and opened[3][3:7] == "#  #"  # never through a building
    assert sum(ch not in CARVABLE for row in opened for ch in row) > sum(
        ch not in CARVABLE for row in rows for ch in row
    )


def _region_north(session, name: str = "The North Woodlot", holder_id=None):
    """A second designed region, north of the first (`_with_region`)."""
    from sealedlore.engine.regions import next_label

    art = session.world_art
    origin = (art.region_origin[0], art.region_origin[1])
    i, j = cell_of(origin, 800.0, 800.0)
    design = {**DESIGN, "name": name, "places": [], "things": [], "people": []}
    region, patch = build_region(
        parse_region(json.dumps(design)),
        key=cell_key(i, j - 1),
        rect=cell_rect(origin, i, j - 1),
        holder_id=holder_id,
        labels_from=next_label(art),
    )
    art.regions[region.key] = region
    art.terrain.append(patch)
    session._area_cache = None
    session._designed_cache = None
    return region


@pytest.mark.parametrize("holder", [None, "erg"])
def test_the_read_never_puts_someone_in_another_region_unless_the_passage_names_it(
    tmp_path, story, cast, holder
):
    """The live test (Oct 18 2026): John went out to check his fields, the
    read was shown every designed region and took "The fields were empty."
    for the North Woodlot next door, and the map had him there. Also with
    the regions inside the story's own region (the Great Erg here; the
    Silverwood, a delta), which reaches every cell: counting it as a town
    the person walks about in let the misread through again."""
    misread = {"where": [{"who": "Serrik", "place": "The North Woodlot", "quote": "were empty"}]}
    session = open_session(tmp_path, story, cast, ["The fields were empty.", json.dumps(misread)])
    here = _with_region(session, holder_id=holder)
    north = _region_north(session, holder_id=holder)
    assert session.world().places[north.place_id].inside == holder
    list(session.send(turn("I go out to check the fields.")))
    asked = session.provider.requests[-1].messages[-1].text
    # Shown only the region they stand in, with what is in it.
    assert "The Kessler fields" in asked and "the Kessler barn" in asked
    assert "North Woodlot" not in asked
    world = session.world()
    assert world.where["Serrik Vaun"] == here.place_id != north.place_id
    me = session.path()[-1].meta.area.spots["Serrik Vaun"]
    assert session.area(me.area).place_id == here.place_id


@pytest.mark.parametrize("holder", [None, "erg"])
def test_a_passage_that_names_the_next_region_takes_them_into_it(tmp_path, story, cast, holder):
    read = {"where": [{"who": "Serrik", "place": "The North Woodlot", "quote": "the woodlot"}]}
    session = open_session(
        tmp_path, story, cast, ["He walked north into the woodlot.", json.dumps(read)]
    )
    _with_region(session, holder_id=holder)
    north = _region_north(session, holder_id=holder)
    list(session.send(turn("I walk north to the woodlot.")))
    assert session.world().where["Serrik Vaun"] == north.place_id
    me = session.path()[-1].meta.area.spots["Serrik Vaun"]
    area = session.area(me.area)
    assert area.place_id == north.place_id  # stood in it, not left in the region below
    assert area.rect[1] == north.north


def test_a_region_the_read_can_t_see_is_still_found_by_its_name_and_someone_new_has_none():
    from sealedlore.engine.regions import for_the_read
    from sealedlore.engine.world import resolve_reply

    world, art, region, _patch = designed()
    origin = (art.region_origin[0], art.region_origin[1])
    folded = world_at([], None, designed=designed_delta(art))
    west, north, side = cell_rect(origin, 0, 0)
    inside = (west + 100.0, north - 100.0)
    shown = for_the_read(folded, art, inside)
    assert region.place_id in shown.places and region.people[0].label in shown.where
    away = for_the_read(folded, art, (west + side + 100.0, north - 100.0))
    assert not {p.id for p in region.places} & set(away.places)
    assert not away.where and not away.unknowns and not away.things
    # Someone standing in the next region east: the barn is elsewhere, and
    # named by the passage it is where they are; someone new is never held.
    east = cell_rect(origin, 1, 0)
    source = "He stood by the fence. Then he went to the barn."
    for quote, kept in (("stood by the fence", False), ("went to the barn", True)):
        reply = {"where": [{"who": "Serrik Vaun", "place": "the Kessler barn", "quote": quote}]}
        delta, refused = resolve_reply(
            folded, reply, source=source, people=[], standing_in={"Serrik Vaun": east}
        )
        assert ("Serrik Vaun" in delta.where) is kept and bool(refused) is not kept
    reply = {"where": [{"who": "a woman in grey", "place": "the Kessler barn", "quote": "fence"}]}
    delta, refused = resolve_reply(
        folded, reply, source=source, people=[], standing_in={"Serrik Vaun": east}
    )
    assert delta.unknowns and not refused
