"""A journey from a village to a market town and back, played live (Oct 18
2026, a story drafted from a premise, 20 turns on Sonnet 4.6): what it found
wrong on the map, each held here."""

from __future__ import annotations

import json

import pytest

from sealedlore.engine.area import BRIDGE, build_area, tile_point
from sealedlore.engine.session import SessionNotice
from sealedlore.engine.takeable import the
from sealedlore.engine.tile_sets import closest_item
from sealedlore.engine.world import applied, place_delta, resolve_reply
from sealedlore.engine.world_art import (
    covering_plan,
    plan_art,
    plan_place,
    plan_tile,
    rasterise,
)
from sealedlore.engine.world_layout import metres
from sealedlore.models.character import Character
from sealedlore.models.world import PlaceLayout, WorldArt, WorldDelta, WorldMap, WorldPlace
from tests.test_area import make_session, turn

WREN = Character(id="w", name="Wren Halloway")


def laid_out(spec: dict, place: WorldPlace, *more: WorldPlace) -> tuple[WorldMap, WorldArt]:
    world = WorldMap(places={p.id: p for p in (place, *more)}, seen=[place.id])
    art = WorldArt()
    rows, objects, labels = rasterise(spec, 40, 24)
    art.layouts[place.id] = {
        "": PlaceLayout(
            place_id=place.id, spec=spec, rows=rows, objects=objects, labels=labels, made_at="1"
        )
    }
    return world, art


# --- the plan's own tiles ----------------------------------------------------------


def test_a_road_over_water_is_a_bridge_and_its_arches_are_its_deck():
    """The toll bridge's road stopped at both banks and its "three stone
    arches" stood in the river as crates: nobody could cross."""
    spec = {
        "ground": "grass",
        "features": [
            {"kind": "area", "fill": "water", "shape": "rect", "x": 15, "y": 0, "w": 6, "h": 24},
            {"kind": "road", "points": [[0, 12], [39, 12]], "width": 1},
            {"kind": "object", "name": "stone arch", "x": 16, "y": 12},
            {"kind": "object", "name": "stone pier", "x": 18, "y": 12},
            {"kind": "object", "name": "toll gate", "x": 30, "y": 12},
        ],
    }
    bridge = WorldPlace(id="b", name="The toll bridge", kind="site", x=0.0, y=0.0, size=60.0)
    world, art = laid_out(spec, bridge)
    area = build_area(world, art, "b")
    row = next(r for r in area.rows if BRIDGE in r)
    assert "~" not in row.strip(".=,")  # the road goes over, deck all the way
    assert {p.name for p in area.placements} == {"toll gate"}  # the arches are the bridge
    y = area.rows.index(row)
    x0, x1 = row.index(BRIDGE), row.rindex(BRIDGE)
    assert area.walkable(x0 - 1, y) and area.walkable(x1 + 1, y)
    assert all(area.walkable(x, y) for x in range(x0, x1 + 1))


def test_rock_ground_out_of_doors_is_walked_on_and_a_road_is_cut_through_rock():
    """The replay (Oct 18 2026): the toll bridge's plan came with "ground":
    "rock" (every tile solid, nobody could take a step) and its stone bridge
    as a rock fill over the river, laid after the road: no way across."""
    spec = {
        "ground": "rock",
        "features": [
            {"kind": "area", "fill": "water", "shape": "rect", "x": 0, "y": 9, "w": 40, "h": 7},
            {"kind": "road", "points": [[19, 23], [19, 1]], "width": 1},
            {"kind": "area", "fill": "rock", "shape": "rect", "x": 17, "y": 9, "w": 5, "h": 8},
        ],
    }
    bridge = WorldPlace(id="b", name="the old toll bridge", kind="site", x=0.0, y=0.0, size=60.0)
    world, art = laid_out(spec, bridge)
    area = build_area(world, art, "b")
    assert area.look == "rock"
    counts = {ch: sum(row.count(ch) for row in area.rows) for ch in "^.="}
    assert counts["."] > counts["^"]  # rocky ground to walk on; the fill stays rock
    road = [x for x, ch in enumerate(area.rows[area.height // 2]) if ch == "="]
    assert road  # the road runs through the rock in the river
    north, south = (road[0], 1), (road[0], area.height - 2)
    assert area.walkable(*north) and area.walkable(*south)
    # Under the ground, rock is solid and only a passage goes through.
    cave = WorldPlace(id="c", name="the Hidden Caves", kind="site", x=0.0, y=0.0, size=60.0)
    passage = {"kind": "passage", "points": [[0, 12], [39, 12]], "width": 2}
    world, art = laid_out({"ground": "rock", "features": [passage]}, cave)
    rows = build_area(world, art, "c").rows
    assert "." not in "".join(rows) and "^" in rows[0] and "_" in rows[len(rows) // 2]


def test_a_door_or_window_drawn_as_a_thing_in_a_wall_is_that_door_or_window():
    """The map shop's "door to the lane" stood in its south wall as a thing,
    drawn with a picture of its own, and the storyteller was told it was
    fixed in place when the author locked it."""
    spec = {
        "ground": "floor",
        "features": [
            {"kind": "building", "x": 0, "y": 0, "w": 40, "h": 24, "doors": []},
            {"kind": "object", "name": "door to the lane", "x": 20, "y": 23},
            {"kind": "object", "name": "the front window", "x": 0, "y": 10},
            {"kind": "object", "name": "a drafting table", "x": 10, "y": 10},
        ],
    }
    shop = WorldPlace(id="s", name="The map shop", kind="building", x=0.0, y=0.0, size=15.0)
    world, art = laid_out(spec, shop)
    area = build_area(world, art, "s")
    assert area.rows[23][20] == "+" and area.rows[10][0] == "W"
    assert [p.name for p in area.placements] == ["a drafting table"]
    assert (20, 23) in area.exits


# --- what the read says, held in code -------------------------------------------------

OPENING = (
    "Wren has spread the maps across the drafting table. His desk at the back is as he left "
    "it. Tobias raps on the glass from the lane outside."
)


def test_furniture_the_read_lists_as_a_place_stands_for_the_building_it_is_in():
    reply = {
        "places": [
            {"name": "map shop", "kind": "building", "quote": "His desk at the back"},
            {"name": "drafting table", "kind": "area", "inside": "map shop", "quote": OPENING[:40]},
            {"name": "Aldous's desk", "kind": "area", "inside": "map shop", "quote": "His desk"},
        ],
        "links": [{"from": "drafting table", "to": "map shop", "direction": "in", "quote": "His"}],
        "where": [{"who": "Wren Halloway", "place": "drafting table", "quote": "Wren has spread"}],
        "things": [{"name": "the coffee cup", "at": "Aldous's desk", "quote": "His desk"}],
    }
    delta, refused = resolve_reply(WorldMap(), reply, source=OPENING, people=[WREN])
    assert [p.name for p in delta.places] == ["map shop"]
    shop = delta.places[0].id
    assert delta.where == {"Wren Halloway": shop}
    assert delta.things[0].at == shop
    assert delta.links == []
    assert sum("furniture, not a place" in r for r in refused) == 2


def test_a_town_only_named_waits_until_the_story_says_where_it_is():
    """ "Road goes to Saltmarsh" put the town 1.2 km west of the toll bridge,
    a few hours south in the story, and places never move."""
    world = WorldMap(
        places={"b": WorldPlace(id="b", name="the toll bridge", kind="site", x=0.0, y=0.0)},
        seen=["b"],
    )
    said = '"Road goes to Saltmarsh," Fen said.'
    reply = {
        "places": [{"name": "Saltmarsh", "kind": "settlement", "quote": "Road goes to Saltmarsh"}],
        # Its direction isn't in the quote: dropped, so nothing says where.
        "links": [
            {"from": "the toll bridge", "to": "Saltmarsh", "direction": "south", "quote": "Road"}
        ],
    }
    delta, refused = resolve_reply(world, reply, source=said, people=[])
    assert delta.places == [] and delta.links == []
    assert refused == ["place Saltmarsh: only named, nothing says where it is yet"]
    # Given a way and a distance, or someone there, it is a place.
    far = "Saltmarsh lies three hours south of the toll bridge."
    reply["links"][0].update(distance="three hours", quote=far)
    reply["places"][0]["quote"] = far
    delta, refused = resolve_reply(world, reply, source=far, people=[])
    assert [p.name for p in delta.places] == ["Saltmarsh"] and refused == []
    there = "Wren walked into Saltmarsh at noon."
    reply = {
        "places": [{"name": "Saltmarsh", "kind": "settlement", "quote": there}],
        "where": [{"who": "Wren Halloway", "place": "Saltmarsh", "quote": there}],
    }
    delta, _ = resolve_reply(world, reply, source=there, people=[WREN])
    assert [p.name for p in delta.places] == ["Saltmarsh"]


def test_any_place_only_named_waits_and_one_stood_in_or_beside_another_is_placed():
    """The replay (Oct 18 2026): "whether the weir pool was running deep",
    asked in the map shop, set the pool 500 m from it, when the story has it
    half an hour past the toll bridge, and places never move. Not only towns
    wait now; a bare link (no way, no distance) says nothing of where."""
    shop = WorldPlace(id="shop", name="map shop", kind="building", x=0.0, y=0.0)
    world = WorldMap(places={"shop": shop}, where={"Wren Halloway": "shop"}, seen=["shop"])
    said = '"He asked whether the weir pool was running deep, down by the landing."'
    reply = {
        "places": [
            {"name": "the weir pool", "kind": "area", "quote": "the weir pool"},
            {"name": "the landing", "kind": "area", "quote": "the landing"},
        ],
        "links": [{"from": "the landing", "to": "the weir pool", "quote": "down by the landing"}],
    }
    delta, refused = resolve_reply(world, reply, source=said, people=[WREN])
    assert delta.places == [] and delta.links == []
    assert sorted(refused) == [
        "place the landing: only named, nothing says where it is yet",
        "place the weir pool: only named, nothing says where it is yet",
    ]
    # Beside a place ("behind the shop"), up a stair from one, or with
    # someone there: where it is.
    for link in ({"direction": "behind"}, {"direction": "down"}):
        said = "The weir pool lay behind the shop, down the steps."
        reply = {
            "places": [{"name": "the weir pool", "kind": "area", "quote": said}],
            "links": [{"from": "map shop", "to": "the weir pool", "quote": said, **link}],
        }
        assert [p.name for p in resolve_reply(world, reply, source=said, people=[])[0].places] == [
            "the weir pool"
        ], link
    # The author's Direction adds a place where it is said, as ever.
    said = "There is a mill on the hill."
    reply = {"places": [{"name": "the mill", "kind": "building", "quote": said}]}
    delta, _ = resolve_reply(world, reply, source=said, people=[], author_says=True)
    assert [p.name for p in delta.places] == ["the mill"]


def test_another_name_for_a_road_the_map_has_is_that_road():
    """The walk out of the village made "river road south"; the walk back,
    "the river road" beside it: two routes for one road."""
    road = WorldPlace(id="r", name="river road south", kind="route", x=0.0, y=-900.0)
    lane = WorldPlace(id="l", name="the lane", kind="route", x=10.0, y=50.0)
    world = WorldMap(places={"r": road, "l": lane}, seen=["r", "l"])
    said = "They walked north on the river road, the Callow loud beside them."
    reply = {
        "places": [
            {"name": "the river road", "kind": "route", "inside": "the valley", "quote": said}
        ],
        "where": [{"who": "Wren Halloway", "place": "the river road", "quote": said}],
    }
    delta, refused = resolve_reply(world, reply, source=said, people=[WREN])
    assert delta.added == [] and refused == []
    assert [(p.id, p.name, p.aliases) for p in delta.places] == [
        ("r", "river road south", ["the river road"])
    ]
    assert delta.where == {"Wren Halloway": "r"}
    # Folded, a later read naming it so finds it by that name.
    folded = applied(world, delta)
    assert "the river road" in folded.places["r"].aliases
    assert resolve_reply(folded, reply, source=said, people=[WREN])[0].added == []
    # The road to a town is the town's road; road words alone name no road,
    # nor do the words of a different one.
    to_town = WorldPlace(id="s", name="the road to Saltmarsh", kind="route")
    world = WorldMap(places={"r": road, "s": to_town})
    for name, found in [
        ("the Saltmarsh road", "s"),
        ("the old river road", "r"),
        ("the north road", None),
        ("the mill road", None),
    ]:
        said = f"They took {name} at dawn."
        entry = {"places": [{"name": name, "kind": "route", "quote": said}]}
        delta, _ = resolve_reply(world, entry, source=said, people=[])
        merged = [p.id for p in delta.places if p.id in ("r", "s")]
        assert merged == ([found] if found else []), name
    # Never into a place that isn't a route, nor for one that isn't.
    world = WorldMap(places={"r": road})
    said = "The river road inn stood by the ford."
    entry = {"places": [{"name": "the river road inn", "kind": "building", "quote": said}]}
    assert [p.id for p in resolve_reply(world, entry, source=said, people=[])[0].places] != ["r"]


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        ("about a quarter-mile", 402),
        ("a quarter mile", 402),
        ("quarter of a mile", 402),
        ("a half-mile", 805),
        ("half a mile", 805),
        ("three-quarters of a mile", 1207),
        ("two-thirds of a mile", 1073),
        ("a quarter of an hour", 1250),
    ],
)
def test_a_share_of_a_unit_is_that_share(words, expected):
    """A boundary stone "about a quarter-mile" south was put 115 m off: the
    words read as nothing, and "quarter of a mile" read as a whole mile."""
    assert metres(words) == pytest.approx(expected, abs=1)


def test_the_way_out_of_a_building_is_at_its_door():
    """ "out" from the shop to its lane put the lane a hundred metres off."""
    world = WorldMap()
    text = "Outside, the lane was grey."
    reply = {
        "places": [
            {"name": "map shop", "kind": "building", "size": 15, "quote": text},
            {"name": "the lane", "kind": "area", "quote": text},
        ],
        "links": [{"from": "map shop", "to": "the lane", "direction": "out", "quote": text}],
    }
    delta, _ = resolve_reply(world, reply, source=text, people=[])
    place_delta(world, delta, None, None)
    shop, lane = delta.places
    gap = ((shop.x - lane.x) ** 2 + (shop.y - lane.y) ** 2) ** 0.5
    assert gap <= (shop.size + lane.size) / 2 + 1


def test_a_thing_s_name_takes_no_the_before_an_owner():
    assert the("Aldous's coffee cup") == "Aldous's coffee cup"
    assert the("his lantern") == "his lantern"
    assert the("the lamp table") == the("A lamp table") == "the lamp table"


# --- small places on the ground --------------------------------------------------------


def test_a_small_place_out_of_doors_is_planned_at_walking_scale_a_room_is_not():
    stone = WorldPlace(id="s", name="the boundary stone", kind="area", size=3.0)
    room = WorldPlace(id="r", name="the pantry", kind="room", size=3.0)
    assert plan_tile(stone) == pytest.approx(1.5)
    assert plan_tile(room) == pytest.approx(0.5)


def test_a_feature_by_a_plan_already_drawn_stands_on_it_and_gets_none_of_its_own():
    """A boundary stone, a marker post and a cairn within eight metres of each
    other each got a plan: the same ground drawn three times, three ways."""
    spec = {"ground": "grass", "features": [{"kind": "object", "name": "cairn", "x": 22, "y": 14}]}
    stone = WorldPlace(id="s", name="the boundary stone", kind="area", x=0.0, y=0.0, size=3.0)
    cairn = WorldPlace(id="c", name="the cairn", kind="area", x=4.0, y=-6.0, size=3.0)
    world, art = laid_out(spec, stone, cairn)
    assert covering_plan(world, art, cairn) is stone
    assert plan_place(world, art, "c") is stone
    world.where["Wren Halloway"] = "c"
    world.visited["Wren Halloway"] = ["s", "c"]
    jobs = plan_art(world, art, here="c", setting="the setting", limit=50)
    assert not any(j.key.startswith("layout:c") for j in jobs)
    # Far from any plan, it is planned as before (the ground round it).
    cairn.x, cairn.y = 900.0, 900.0
    assert covering_plan(world, art, cairn) is None


def test_someone_at_a_place_the_plan_doesn_t_mark_stands_at_its_spot(tmp_path, story, cast):
    """The eel-seller's stall had no label on the town's plan, and she stood
    at the town's way in, 235 m from the stall she was selling at."""
    session = make_session(tmp_path, story, cast)
    world = session.world()
    farm = session.area("farm")
    # On the farm's ground, not one of its places: the plan has no label for it.
    world.places["shrine"] = WorldPlace(
        id="shrine", name="the shrine", kind="area", x=-100.0, y=60.0, size=3.0
    )
    spot = session._spot_in(world, farm, "shrine")
    assert spot is not None and spot != session._way_in(farm)
    state = session.area_state()
    world.where["Maela Orr"] = "shrine"
    art = session.map_art_copy()
    assert plan_place(world, art, "shrine").id == "farm"  # a feature on the farm's plan
    stood = session.settle(state.model_copy(deep=True), world, art)
    maela = stood.spots["Maela Orr"]
    assert abs(maela.x - spot[0]) + abs(maela.y - spot[1]) <= 3


# --- the reads together ---------------------------------------------------------------


def test_a_newcomer_the_scene_read_names_is_the_one_the_map_kept_unnamed(tmp_path, story, cast):
    """The map shop's dealer stepped out "a woman, perhaps forty": the map
    kept her as Unknown 3, the scene read knew her for Sable Cours, and both
    stood in the shop."""
    passage = "A woman with a ring of keys came in from the yard and nodded."
    scene = {"arrived": [{"name": "Maela Orr", "how": "came in from the yard"}], "left": []}
    read = {
        "where": [{"who": "a woman with a ring of keys", "place": "The house", "quote": passage}]
    }
    story.scene.present_character_ids = ["char-serrik"]
    session = make_session(tmp_path, story, cast, [passage, json.dumps(scene), json.dumps(read)])
    session.config.scene_reads = "every_turn"
    sent = list(session.send(turn("Is that you, Maela Orr?")))
    notices = [n.text for n in sent if isinstance(n, SessionNotice)]
    node = session.path()[-1]
    assert node.meta.world is not None and not node.meta.world.unknowns
    assert node.meta.world.where.get("Maela Orr") == "house"
    spots = node.meta.area.spots
    assert "Maela Orr" in spots and not any(n.startswith("Unknown") for n in spots)
    assert "Map: the newcomer is Maela Orr." in notices


def test_a_read_that_moves_the_author_s_character_within_the_area_moves_them(tmp_path, story, cast):
    """Read at the north gate, Wren stayed at the counting house's door
    across the town: the author's character was kept wherever they stood in
    the area, even when a read had moved them."""
    session = make_session(tmp_path, story, cast)
    world, art = session.world(), session.map_art_copy()
    state = session.area_state()
    session.walked(session.walk_route(*next(e for e in session.area("house").exits)[:2]))
    state = session.go_through()
    me = state.spots["Serrik Vaun"]
    assert me.area == "farm"
    world = session.world()
    farm = session.area("farm")
    far = max(
        ((x, y) for y in range(farm.height) for x in range(farm.width) if farm.walkable(x, y)),
        key=lambda t: abs(t[0] - me.x) + abs(t[1] - me.y),
    )
    state.spots["Serrik Vaun"] = me.model_copy(update={"x": far[0], "y": far[1]})
    world.where["Serrik Vaun"] = "house"
    world.outside.append("Serrik Vaun")
    kept = session.settle(state.model_copy(deep=True), world, art, walked=True)
    assert (kept.spots["Serrik Vaun"].x, kept.spots["Serrik Vaun"].y) == far
    moved = session.settle(state.model_copy(deep=True), world, art, walked=False)
    door = next(e for e in farm.entrances if e.place_id == "house")
    spot = moved.spots["Serrik Vaun"]
    assert abs(spot.x - door.x) + abs(spot.y - door.y) <= 3


# --- the pictures ---------------------------------------------------------------------


def test_a_stand_in_is_the_same_kind_of_thing_before_one_with_the_word():
    """A boundary stone stood in as the set's "large stone hearth"."""
    slugs = ["large-stone-hearth", "a-mossy-standing-stone"]
    assert closest_item("boundary stone", slugs) == "a-mossy-standing-stone"
    assert closest_item("boundary stone", ["large-stone-hearth"]) == "large-stone-hearth"


def test_the_play_view_roofs_a_building_its_finer_tiles_make_wide():
    """At a lane's 1.5 m a tile a 27 m shop was drawn roofless, grass inside."""
    from sealedlore.gui.map_tiles import building_boxes

    rows = ["." * 30] + ["." + "#" * 20 + "." * 9] + ["." + "#" + " " * 18 + "#" + "." * 9] * 8
    rows += ["." + "#" * 9 + "+" + "#" * 10 + "." * 9, "." * 30]
    assert building_boxes(rows) == []
    assert building_boxes(rows, most=(39, 29)) == [(1, 20, 1, 10)]


def test_someone_coming_into_a_building_stands_inside_its_door_not_a_gap_in_its_wall():
    """The map shop's keeper stood in a gap of its north wall at the start,
    and again on coming home; its door is in the south wall."""
    from sealedlore.engine.area import inside_door

    spec = {
        "ground": "floor",
        "features": [
            {"kind": "building", "x": 0, "y": 0, "w": 40, "h": 24, "doors": []},
            {"kind": "area", "fill": "floor", "x": 7, "y": 0, "w": 3, "h": 1},
            {"kind": "object", "name": "door to the lane", "x": 20, "y": 23},
        ],
    }
    shop = WorldPlace(id="s", name="The map shop", kind="building", x=0.0, y=0.0, size=15.0)
    world, art = laid_out(spec, shop)
    area = build_area(world, art, "s")
    assert inside_door(area) == (20, 22)


def test_the_opening_s_read_is_told_where_the_setup_begins_and_may_quote_it(tmp_path, story, cast):
    """A drafted story opened in a map shop "in Copperbrook", which only the
    setup named: the shop stood alone on the map, its lane a hundred metres
    off, and the village came later, a kilometre away, with nothing in it."""
    story.setup.starting_scene.location = "The mill, Copperbrook"
    line = "The story's setup says it begins at: The mill, Copperbrook."
    reply = {
        "places": [
            {"name": "Copperbrook", "kind": "settlement", "quote": line},
            {"name": "The mill", "kind": "building", "inside": "Copperbrook", "quote": "mill"},
        ],
        "where": [{"who": "Serrik Vaun", "place": "The mill", "quote": "Serrik waits"}],
    }
    session = make_session(tmp_path, story, cast, ["Serrik waits in the mill.", json.dumps(reply)])
    session.story.world_base = None
    session.config.scene_reads = "manual"
    list(session.send(turn("I wait.")))
    asked = session.provider.requests[-1].messages[-1].text
    assert "The story's setup says it begins at: The mill, Copperbrook." in asked
    world = session.world()
    mill = next(p for p in world.places.values() if p.name == "The mill")
    assert world.places[mill.inside].name == "Copperbrook"
    # Only the opening's read, and only for a map the story grows.
    assert session.read_begins_at(session.path()) == ""
    session.story.world_base = WorldDelta()
    assert session.read_begins_at([]) == ""


def test_an_avatar_waits_for_a_portrait_that_has_landed_but_is_not_on_its_card_yet(
    tmp_path, story, cast
):
    """A portrait comes back while the worker owns the story and goes on its
    card once the job is done: in between, the map drew the avatar from the
    card's words, and again from the portrait (six avatars paid twice)."""
    from sealedlore.models.image import GeneratedImage

    session = make_session(tmp_path, story, cast)
    assert session.portraits_landed() == set()
    session.pictures.add_images(
        [
            GeneratedImage(
                id="p1",
                size="1024x1024",
                file="images/p1.png",
                prompt="",
                model="m",
                card_id="char-maela",
                card_first=True,
            )
        ]
    )
    assert session.portraits_landed() == {"char-maela"}
    session.story.card_pictures_taken.append("p1")
    assert session.portraits_landed() == set()


def test_a_place_the_read_adds_that_its_parent_s_plan_draws_stands_where_it_is_drawn(
    tmp_path, story, cast
):
    """The replay (Oct 18 2026): the toll bridge's plan drew its toll house
    by the road; the read added "the toll house" when Wren knocked, the map
    put it at the bridge's middle, on the river, its box was drawn there,
    and Wren, at its door, was stood out in open country."""
    session = make_session(tmp_path, story, cast)
    farm = session.world_art.layouts["farm"][""]
    farm.spec = {
        **farm.spec,
        "features": [
            *farm.spec["features"],
            {"kind": "building", "name": "toll house", "x": 4, "y": 4, "w": 4, "h": 4},
        ],
    }
    session._area_cache = None
    world = session.world()
    area = session.area("farm", world, session.map_art_copy())
    drawn = tile_point(world, area, *area.labels["toll house"])
    delta = WorldDelta(
        places=[WorldPlace(name="the toll house", kind="building", inside="farm")], added=[]
    )
    delta.added = [delta.places[0].id]
    session._where_drawn(world, delta)
    assert (delta.places[0].x, delta.places[0].y) == pytest.approx(drawn)
    # Given no parent: found in the plan where the author's character is,
    # which is then its parent.
    world.where["Serrik Vaun"] = "farm"
    loose = WorldPlace(name="Toll House", kind="building")
    delta = WorldDelta(places=[loose], added=[loose.id])
    session._where_drawn(world, delta, "Serrik Vaun")
    assert (loose.x, loose.y) == pytest.approx(drawn) and loose.inside == "farm"
    # Nothing drawn of that name: left for the layout as before.
    other = WorldPlace(name="the mill", kind="building", inside="farm")
    delta = WorldDelta(places=[other], added=[other.id])
    session._where_drawn(world, delta)
    assert other.x is None


def test_outside_a_house_with_no_plan_of_its_own_they_stand_on_the_ground_round_it(
    tmp_path, story, cast
):
    """The replay (Oct 18 2026): Wren knocked at the toll house, which had no
    plan of its own; the toll bridge, a site, was taken for the building she
    was outside of, and she was stood outside the whole bridge, in open
    country."""
    session = make_session(tmp_path, story, cast)
    world, art = session.world(), session.map_art_copy()
    shed = WorldPlace(id="shed", name="The shed", kind="building", inside="farm", x=-60.0, y=40.0)
    world.places["shed"] = shed
    world.where["Serrik Vaun"] = "shed"
    world.outside = ["Serrik Vaun"]
    state = session.settle(session.area_state(), world, art, walked=False)
    assert state.spots["Serrik Vaun"].area == "farm"
