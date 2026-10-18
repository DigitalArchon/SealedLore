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
    for _ in range(40):
        x, y = thing_tile(area, (80, 80), taken)
        assert abs(x - 80) <= 24 and abs(y - 80) <= 24
        taken.add((x, y))
    assert time.perf_counter() - t0 < 2.0


def test_the_next_morning_moves_the_map_s_clock_to_the_next_day():
    from sealedlore.engine.chronicle_read import stated_skip

    now = 16 * 60 + 35  # day 1, 16:35
    told = "The next morning, with the tide low, the skiff carries them out."
    moved = stated_skip(told, now, next_time=True)
    assert moved is not None and moved[0] == 24 * 60 + 7 * 60
    # The plot's clock is as measured: its read after the passage says so.
    assert stated_skip(told, now) is None
    for text in (
        "She hopes the next morning will bring news.",
        "She had left the next morning, and never come back.",
        "Years ago, the next morning was always market day.",
    ):
        assert stated_skip(text, now, next_time=True) is None, text
    breakfast = stated_skip(
        "The next morning they had breakfast by the river.", now, next_time=True
    )
    assert breakfast is not None and breakfast[0] == 24 * 60 + 7 * 60
    # Before dawn the night isn't over: the next morning is this day's.
    small_hours = stated_skip(told, 2 * 60, next_time=True)
    assert small_hours is not None and small_hours[0] == 7 * 60


def test_a_shortened_thing_name_matches_only_near_the_reply_s_people():
    from sealedlore.engine.world import _find_thing
    from sealedlore.models.world import WorldThing

    marsh = WorldPlace(name="the Cull Marsh", kind="region", x=500.0, y=0.0, size=400.0)
    bar = WorldPlace(name="the sandbar", kind="area", x=900.0, y=-1400.0, size=120.0)
    cattle = WorldThing(name="a cattle bell, iron, its clapper missing", at=marsh.id)
    world = WorldMap(places={marsh.id: marsh, bar.id: bar}, things={cattle.id: cattle})
    assert _find_thing(world, "the bell") is cattle  # as before, with no places to go by
    assert _find_thing(world, "the bell", {bar.id}) is None
    assert _find_thing(world, "the bell", {marsh.id}) is cattle


def test_a_small_vehicle_has_no_inside_and_a_moored_one_is_laid_at_its_dock():
    from sealedlore.engine.area import LaidThing, lay_things
    from sealedlore.engine.world_art import small_vehicle

    skiff = WorldPlace(name="Maret's skiff", kind="vehicle", size=6.0)
    barge = WorldPlace(name="Dav's barge", kind="vehicle", size=25.0)
    assert small_vehicle(skiff) and not small_vehicle(barge)
    # As the playtest had it: "a small boat" read with no size, given the
    # kind's 25 m, and drawn as a boathouse inside.
    kept = WorldPlace(name="Maret's skiff", kind="vehicle", what="a small boat", size=25.0)
    assert small_vehicle(kept)
    new = WorldPlace(name="Maret's skiff", kind="vehicle", what="a small boat")
    place_new(WorldMap(), [new], [], None, (0.0, 0.0))
    assert new.size is not None and new.size <= 12
    carriage = WorldPlace(name="the rail carriage", kind="vehicle")
    place_new(WorldMap(), [carriage], [], None, (0.0, 0.0))
    assert not small_vehicle(carriage)
    rows = ["." * 20 for _ in range(12)]
    area = Area(
        place_id="q",
        name="quay",
        indoor=False,
        look="grass",
        rows=rows,
        tile_m=9.0,
        objects={},
        labels={},
    )
    laid = lay_things(
        area, [LaidThing(id="b", name="Dav's barge", x=4, y=5, w=3, h=1, vehicle=True)]
    )
    assert [(p.name, p.w, p.h, p.kind) for p in laid.placements] == [
        ("Dav's barge", 3, 1, "vehicle")
    ]
    assert laid.labels["Dav's barge"] == (5, 6)
    # A label is never off the grid.
    low = lay_things(area, [LaidThing(id="b", name="Dav's barge", x=4, y=11, w=3, vehicle=True)])
    assert low.labels["Dav's barge"] == (5, 11)


def test_regions_beyond_the_next_leave_the_storyteller_s_tail():
    from sealedlore.engine.world import render_map_context

    town = WorldPlace(name="Marrowgate", kind="settlement", x=0.0, y=0.0, size=1500.0)
    near = WorldPlace(
        name="the Cull Marsh",
        kind="region",
        what="A broad wedge of marsh.",
        x=600.0,
        y=0.0,
        size=400.0,
    )
    far = WorldPlace(
        name="the Holt Ground",
        kind="region",
        what="Old common ground.",
        x=-1000.0,
        y=1000.0,
        size=400.0,
    )
    world = WorldMap(places={town.id: town, near.id: near, far.id: far}, where={"Ines": town.id})
    text = render_map_context(world, "Ines", left_out={far.id}, brief={near.id})
    assert "Holt Ground" not in text
    assert "the Cull Marsh (region" in text and "broad wedge" not in text


def test_only_the_company_goes_together_where_one_of_them_may_go():
    """A place one of the company may go is within reach of the rest of the
    company (Adelaide into the carriage Silas stood by, her own spot three
    kilometres behind); someone outside the company standing there opens it
    to nobody, and the read's short names are the map's own."""
    from sealedlore.engine.world import resolve_reply
    from sealedlore.models.character import Character

    shop = WorldPlace(name="The workshop", kind="building", x=0.0, y=0.0, size=20.0)
    carriage = WorldPlace(name="The rail carriage", kind="site", x=3000.0, y=0.0, size=30.0)
    world = WorldMap(
        places={shop.id: shop, carriage.id: carriage},
        where={"Adelaide Voss": shop.id, "Silas Brack": carriage.id, "Unknown 101": carriage.id},
        unknowns={"Unknown 101": "a porter in a grey coat"},
    )
    people = [Character(id="a", name="Adelaide Voss"), Character(id="s", name="Silas Brack")]
    standing_in = {
        "Adelaide Voss": (-200.0, 200.0, 400.0),
        "Silas Brack": (2800.0, 200.0, 400.0),
        "Unknown 101": (2800.0, 200.0, 400.0),
    }
    source = "He sat by the window. She sat beside him."

    def read(first: str, together):
        reply = {
            "where": [
                {"who": first, "place": "The rail carriage", "quote": "He sat by the window."},
                {"who": "Adelaide", "place": "The rail carriage", "quote": "She sat beside him."},
            ]
        }
        delta, _refused = resolve_reply(
            world, reply, source=source, people=people, standing_in=standing_in, together=together
        )
        return delta.where.get("Adelaide Voss")

    assert read("Silas", {"Adelaide Voss", "Silas Brack"}) == carriage.id
    assert read("Silas", ()) is None  # no company told: each on their own
    # The porter by the carriage isn't in Adelaide's company.
    assert read("Unknown 101", {"Adelaide Voss", "Silas Brack"}) is None


def test_a_tile_set_shares_another_s_things_without_copies(tmp_path):
    """The abandoned town has Doomsville's 194 things through its manifest
    (`items_from`), not a second copy of each picture; one set only, by a
    plain name beside it."""
    import json

    from sealedlore.storage.tile_sets import asset_names, read_asset

    def make(name: str, items: dict[str, bytes], shares: str = "") -> object:
        folder = tmp_path / name
        (folder / "items").mkdir(parents=True)
        (folder / "tileset.json").write_text(json.dumps({"name": name, "items_from": shares}))
        for slug_, data in items.items():
            (folder / "items" / f"{slug_}.webp").write_bytes(data)
        return folder

    make("c", {"a-lantern": b"c"})
    make("b", {"a-chair": b"b-chair", "a-stove": b"b-stove"}, shares="c")
    a = make("a", {"a-chair": b"a-chair"}, shares="b")
    assert read_asset(a, "items/a-chair") == b"a-chair"  # its own first
    assert read_asset(a, "items/a-stove") == b"b-stove"
    assert read_asset(a, "items/a-lantern") is None  # never a share of a share
    assert read_asset(a, "floor") is None  # things only
    assert asset_names(a, "items") == ["items/a-chair", "items/a-stove"]
    odd = make("d", {}, shares="../b")
    assert read_asset(odd, "items/a-stove") is None


def test_a_moored_barge_lies_afloat_named_on_the_shore_and_is_boarded_there(tmp_path, story, cast):
    """The playtest (Oct 18 2026): Dav's barge and its bargeman were
    invisible on the quay. Laid on the water by the landing even with no
    other thing on the map, as long as it is (up to a cap), its name and
    its gangway on the shore beside it; through the gangway to its inside
    and back out by it."""
    from sealedlore.engine.world_art import rasterise
    from sealedlore.models.area import Spot
    from sealedlore.models.world import PlaceLayout
    from tests.test_area import FARM_SPEC, make_session

    session = make_session(tmp_path, story, cast)
    art = session.world_art
    river = {**FARM_SPEC, "features": [*FARM_SPEC["features"],
             {"kind": "area", "fill": "water", "x": 34, "y": 0, "w": 6, "h": 24}]}  # fmt: skip
    rows, objects, labels = rasterise(river, 40, 24)
    art.layouts["farm"] = {
        "": PlaceLayout(place_id="farm", spec=river, rows=rows, objects=objects, made_at="2")
    }
    deck = {"ground": "floor", "features": [
        {"kind": "building", "x": 0, "y": 0, "w": 30, "h": 8, "doors": [{"side": "west", "at": 4}]}
    ]}  # fmt: skip
    rows, objects, labels = rasterise(deck, 30, 8)
    art.layouts["barge"] = {
        "": PlaceLayout(place_id="barge", spec=deck, rows=rows, objects=objects, made_at="1")
    }
    landing = WorldPlace(
        id="landing", name="The landing", kind="area", inside="farm", x=125.0, y=0.0, size=10.0
    )
    barge = WorldPlace(
        id="barge", name="Dav's barge", kind="vehicle", inside="landing", size=25.0,
        what="A flat river barge.",
    )  # fmt: skip
    session.story.world_base.places += [landing, barge]
    session.story.world_base.where["Serrik Vaun"] = "farm"
    session._area_cache = None
    area = session.area("farm")
    laid = next(p for p in area.placements if p.id == "barge")
    assert laid.kind == "vehicle" and laid.w == min(round(25 / area.tile_m), 16) > 1
    assert all(area.at(x, y) == "~" for x, y in laid.tiles())  # afloat
    gx, gy = area.labels["Dav's barge"]
    assert area.walkable(gx, gy)  # named on the shore beside it
    gangway = area.entrance_at(gx, gy)
    assert gangway is not None and gangway.place_id == "barge"
    # Aboard by the gangway, and back out by it.
    state = session.area_state().model_copy(deep=True)
    state.spots["Serrik Vaun"] = Spot(area="farm", x=gx, y=gy)
    session._set_live(state)
    aboard = session.go_through()
    assert aboard is not None and aboard.spots["Serrik Vaun"].area == "barge"
    inside = session.area("barge")
    ex, ey = inside.exits[0]
    state = session.area_state().model_copy(deep=True)
    state.spots["Serrik Vaun"] = Spot(area="barge", x=ex, y=ey)
    session._set_live(state)
    ashore = session.go_through()
    me = ashore.spots["Serrik Vaun"]
    assert me.area == "farm" and abs(me.x - gx) + abs(me.y - gy) <= 3


def test_a_big_story_never_crowds_the_designer_s_own_names_out():
    from sealedlore.engine.regions import taken_lines
    from tests.test_regions import designed

    _world, art, _region, _patch = designed()
    story = [f"Place {n}" for n in range(200)]
    taken = taken_lines(art, story=story)
    assert "The Kessler fields" in taken and "the Kessler barn" in taken
    assert "Place 0" in taken and "Place 199" not in taken


def test_the_drawn_buildings_are_told_from_where_the_turn_stands(tmp_path, story, cast):
    """`map.drawn` names the plan's buildings the map has no place for when
    the author's character is out on that plan as the turn is written, read
    from the turn's own snapshot: on a retake the story's latest message has
    them elsewhere (indoors, where nothing is told)."""
    from sealedlore.engine.world_art import rasterise
    from sealedlore.models.area import AreaState, Spot
    from sealedlore.models.node import Node, NodeMeta
    from sealedlore.models.world import PlaceLayout
    from tests.test_area import FARM_SPEC, make_session

    session = make_session(tmp_path, story, cast)
    smokehouse = {"kind": "building", "name": "The smokehouse", "x": 2, "y": 2, "w": 5, "h": 4}
    spec = {**FARM_SPEC, "features": [*FARM_SPEC["features"], smokehouse]}
    rows, objects, labels = rasterise(spec, 40, 24)
    session.world_art.layouts["farm"] = {
        "": PlaceLayout(
            place_id="farm", spec=spec, rows=rows, objects=objects, labels=labels, made_at="2"
        )
    }
    session.story.world_base.where["Serrik Vaun"] = "farm"
    session._area_cache = None
    yard = session.area("farm")
    out = AreaState(spots={"Serrik Vaun": Spot(area="farm", x=20, y=yard.height - 2)})
    indoors = AreaState(spots={"Serrik Vaun": Spot(area="house", x=5, y=5)})
    turn_ = Node(
        id="t1", kind="user", speaker_id="char-serrik", content="I look about the yard.",
        meta=NodeMeta(area=out, controlled_character_id="char-serrik"),
    )  # fmt: skip
    session.nodes.append(turn_)
    session._set_live(indoors)  # where the story is now, as on a retake
    told = session.map_context([], "char-serrik", turn_node_id="t1")
    assert "Also drawn on the map of The farm" in told and "The smokehouse" in told
    world = session.world()
    assert session.drawn_context(world, "Serrik Vaun", indoors) == ""
