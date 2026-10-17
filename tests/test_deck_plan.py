"""A station's or ship's decks laid out by the program from the model's
brief (engine/deck_plan.py), wired into the map art (engine/world_art.py)
and the area (engine/area.py), and drawn in the deck look
(gui/deck_look.py)."""

from __future__ import annotations

import json
from collections import deque

import pytest

from sealedlore.engine.area import build_area, build_level
from sealedlore.engine.deck_plan import (
    DECK_TILE_M,
    USES,
    VIEW_USES,
    generate,
    parse_brief,
    use_of,
)
from sealedlore.engine.world_art import ArtJob, deck_layout, finish, plan_art
from sealedlore.models.world import WorldArt, WorldMap, WorldPlace

BRIEF = {
    "shape": "ring",
    "decks": [
        {
            "index": 0,
            "name": "Promenade Deck",
            "rooms": [
                {"name": "The Promenade", "use": "promenade", "size": "huge"},
                {"name": "The Rusty Comet", "use": "bar", "size": "medium"},
                {"name": "Infirmary", "use": "medical", "size": "medium"},
                {"name": "Security Office", "use": "security", "size": "small"},
                {"name": "Chandlery", "use": "cargo", "size": "medium"},
            ],
        },
        {
            "index": 1,
            "name": "Operations",
            "rooms": [
                {"name": "Operations", "use": "command", "size": "large"},
                {"name": "Sensor Lab", "use": "science", "size": "medium"},
            ],
        },
        {
            "index": -1,
            "name": "Docking Ring",
            "rooms": [
                {"name": "Docking Bay 1", "use": "docking", "size": "large"},
                {"name": "Cargo Hold A", "use": "cargo", "size": "large"},
                {"name": "Customs", "use": "security", "size": "medium"},
            ],
        },
    ],
}


def _reachable(rows: list[str], start: tuple[int, int]) -> set[tuple[int, int]]:
    """Every tile a person can reach from `start` over floor, doors and lifts."""
    seen = {start}
    queue = deque([start])
    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if (nx, ny) in seen or not (0 <= ny < len(rows) and 0 <= nx < len(rows[ny])):
                continue
            if rows[ny][nx] in "_+&":
                seen.add((nx, ny))
                queue.append((nx, ny))
    return seen


def test_a_room_s_use_is_read_from_its_words():
    assert use_of("The Rusty Comet bar") == "dining"
    assert use_of("Medbay") == "medical"
    assert use_of("Ops") == "command" and use_of("Main bridge") == "command"
    assert use_of("Fusion core") == "engineering"
    assert use_of("Shuttle bay") == "hangar" and use_of("Airlock 3") == "airlock"
    assert use_of("The Quiet Room") == ""
    assert set(USES) >= VIEW_USES


def test_the_brief_is_held_to_the_rules():
    reply = {
        "shape": "a great wheel",
        "decks": [
            {"index": 2, "name": "Top", "rooms": [{"name": "Bridge", "use": "?", "size": "vast"}]},
            {
                "index": 3,
                "name": "Above",
                "rooms": [
                    {"name": "Cabins", "use": "quarters", "size": "huge"},
                    {"name": "bridge", "use": "command"},  # a name already used
                    {"name": "Lock", "use": "airlock", "size": "large"},
                ],
            },
            {"index": 4, "name": "Empty", "rooms": []},
        ],
    }
    brief = parse_brief(json.dumps(reply), named=(("The Cellar Bar", "a bar"),))
    assert brief.shape == "ring"
    # No deck 0: the nearest becomes it, the rest keep their order.
    assert [d.index for d in brief.decks] == [0, 1]
    main, above = brief.decks
    assert [(r.name, r.use, r.size) for r in main.rooms] == [
        ("Bridge", "command", "medium"),  # its use from its name, an unknown size its use's
        ("The Cellar Bar", "dining", "medium"),  # what the map has in it goes on the main deck
    ]
    assert [(r.name, r.size) for r in above.rooms] == [("Cabins", "medium"), ("Lock", "small")]
    with pytest.raises(ValueError):
        parse_brief('{"decks": [{"index": 0, "rooms": []}]}')


def test_every_deck_lines_up_its_lifts_and_every_room_is_reached():
    plan = generate(parse_brief(json.dumps(BRIEF)), "halcyon")
    assert plan == generate(parse_brief(json.dumps(BRIEF)), "halcyon")  # the same every time
    sizes = {(len(d.rows[0]), len(d.rows)) for d in plan.decks.values()}
    assert len(sizes) == 1  # one frame for every deck
    for deck in plan.decks.values():
        for lx, ly in plan.lifts:
            assert deck.rows[ly][lx] == "&"
        reached = _reachable(deck.rows, plan.lifts[0])
        for room in deck.rooms:
            x0, y0, x1, y1 = room.box
            assert (x0, y0) in reached, (deck.name, room.name)
            assert deck.rows[room.door[1]][room.door[0]] == "+"
            if room.view:
                ring = [deck.rows[y][x] for x in range(x0 - 1, x1 + 2) for y in (y0 - 1, y1 + 1)]
                ring += [deck.rows[y][x] for y in range(y0, y1 + 1) for x in (x0 - 1, x1 + 1)]
                assert "W" in ring, room.name  # a viewport onto space
        # Space round the hull: the frame's edge is never built on.
        assert set(deck.rows[0]) == {" "} and set(deck.rows[-1]) == {" "}
    names = {r.name for d in plan.decks.values() for r in d.rooms}
    assert {"The Promenade", "Operations", "Docking Bay 1"} <= names
    # Ships dock at the main deck and the docking deck: locks with a hatch out.
    for index in (0, -1):
        locks = [r for r in plan.decks[index].rooms if r.use == "airlock"]
        assert locks
        rows = plan.decks[index].rows
        for lock in locks:
            x0, y0, x1, y1 = lock.box
            hatches = [
                (x, y)
                for y in range(y0 - 1, y1 + 2)
                for x in range(x0 - 1, x1 + 2)
                if rows[y][x] == "+" and (x, y) != lock.door
            ]
            assert hatches, lock.name


def test_a_spine_ship_and_a_cross_outpost_lay_out_too():
    for shape in ("spine", "cross"):
        brief = parse_brief(json.dumps({**BRIEF, "shape": shape}))
        plan = generate(brief, f"ship-{shape}")
        assert plan.shape == shape
        for deck in plan.decks.values():
            reached = _reachable(deck.rows, plan.lifts[0])
            assert all((r.box[0], r.box[1]) in reached for r in deck.rooms)


def _station() -> tuple[WorldMap, WorldArt]:
    station = WorldPlace(
        id="st",
        name="Halcyon Reach",
        kind="site",
        what="a ring-shaped trading station in orbit round a gas giant",
        x=0.0,
        y=0.0,
        size=300.0,
    )
    bar = WorldPlace(id="bar", name="The Rusty Comet", kind="building", inside="st", what="a bar")
    world = WorldMap(
        places={"st": station, "bar": bar},
        seen=["st", "bar"],
        visited={"Mara Voss": ["st", "bar"]},
        where={"Mara Voss": "st"},
    )
    return world, WorldArt()


def test_a_station_in_space_is_asked_for_a_brief_and_its_rooms_get_no_plans():
    world, art = _station()
    jobs = plan_art(world, art, here="st", setting="a trading station in deep space")
    layouts = [j for j in jobs if j.kind == "layout"]
    assert [j.key for j in layouts] == ["layout:st"]  # the bar is one of its rooms
    job = layouts[0]
    assert job.extra["deck"] and ("The Rusty Comet", "a bar") in job.extra["named"]
    assert job.extra["ring"]  # "a ring-shaped trading station"
    # A station whose words name no ring is a block whatever the brief says.
    world.places["st"].what = "an independent trading station in orbit round a gas giant"
    plain = next(j for j in plan_art(world, art, here="st", setting="s") if j.kind == "layout")
    assert plain.extra["ring"] is False
    assert finish(plain, json.dumps(BRIEF), model="m", made_at="1").spec["shape"] == "block"
    assert "rooms" in job.messages[0].text and "The Rusty Comet" in job.messages[-1].text
    # A sailing ship is no station: no sign of space, its plan drawn as shapes.
    sloop = WorldPlace(
        id="sl", name="The Sea Sprite", kind="vehicle", what="a sloop with two decks", size=30.0
    )
    sea = WorldMap(places={"sl": sloop}, seen=["sl"], visited={"Ann": ["sl"]}, where={"Ann": "sl"})
    sea_jobs = [j for j in plan_art(sea, WorldArt(), here="sl", setting="the age of sail")]
    assert all(not j.extra.get("deck") for j in sea_jobs if j.kind == "layout")


def test_the_brief_becomes_a_plan_with_its_decks_and_areas_with_named_rooms():
    world, art = _station()
    job = ArtJob(
        "layout:st",
        "layout",
        [],
        "st",
        name="Halcyon Reach",
        extra={"deck": True, "named": [("The Rusty Comet", "a bar")]},
    )
    layout = finish(job, json.dumps(BRIEF), model="m", made_at="1")
    assert layout.spec["deck"] and sorted(lv.index for lv in layout.levels) == [-1, 1]
    art.layouts["st"] = {"": layout}
    area = build_area(world, art, "st")
    assert area is not None and area.indoor and area.look == "space"
    assert area.tile_m == DECK_TILE_M
    uses = {r.name: r.use for r in area.rooms}
    assert uses["The Promenade"] == "promenade" and uses["The Rusty Comet"] == "dining"
    assert "corridor" in uses.values()
    # The ring's open middle stays space: the drawn-plan filler leaves it be.
    assert sum(row.count(" ") for row in area.rows) > sum(
        row.count(" ") for row in layout.rows[:1] + layout.rows[-1:]
    )
    upper = build_level(world, art, "st", 1)
    assert upper is not None and {r.use for r in upper.rooms} >= {"command"}
    assert any(e.stairs for e in area.entrances)  # the lifts lead to the other decks
    # Every deck again from a new brief on a redraw: never the tile shapes.
    art.redraw = ["st"]
    redraw = [j for j in plan_art(world, art, here="st", setting="s") if j.key.endswith("redraw")]
    assert redraw and redraw[0].extra.get("deck")
    assert deck_layout is not None


def test_a_deck_s_corridor_never_takes_a_room_s_use_from_its_label():
    # Live (Oct 17 2026): an L-shaped stretch of a ring's corridor held the
    # customs post's label in its box and was drawn as security, in red.
    from sealedlore.engine.area import Room, name_rooms

    customs = Room(0, "", 24, (15, 30, 20, 33), 17.5, 31.5)
    stretch = Room(1, "", 28, (12, 30, 20, 36), 14.0, 34.0)
    records = [{"name": "Customs and Security Post", "use": "security", "box": [15, 30, 20, 33]}]
    name_rooms([customs, stretch], {"Customs and Security Post": (17, 31)}, records)
    assert (customs.name, customs.use) == ("Customs and Security Post", "security")
    assert stretch.use == "corridor"


def test_a_deck_is_drawn_in_its_rooms_colours_and_a_house_as_before():
    from PySide6.QtWidgets import QApplication

    from sealedlore.engine.tile_sets import TileSetInfo
    from sealedlore.gui.area_render import TileSource, render_area

    _app = QApplication.instance() or QApplication([])  # noqa: F841 - Qt images need it
    world, art = _station()
    job = ArtJob("layout:st", "layout", [], "st", extra={"deck": True, "named": []})
    art.layouts["st"] = {"": finish(job, json.dumps(BRIEF), model="m", made_at="1")}
    area = build_area(world, art, "st")
    source = TileSource(TileSetInfo(name="plain", deck=True), lambda role: None)
    image = render_area(area, source)
    assert image.width() == area.width * 40 and not image.isNull()

    def colour_at(room_name: str):
        room = next(r for r in area.rooms if r.name == room_name)
        return image.pixelColor(int((room.cx + 0.5) * 40), int((room.cy + 0.5) * 40))

    # The infirmary is drawn pale, the security office dark.
    assert colour_at("Infirmary").lightness() > colour_at("Security Office").lightness()
    for room in area.rooms:
        room.use = ""
    plain = render_area(area, source)
    assert plain.size() == image.size() and plain != image


# --- The hull's mass, the bow and stern, the rock, the set pieces --------------------

SHIP = {
    "shape": "spine",
    "hull": "freighter",
    "decks": [
        {
            "index": 0,
            "name": "Main Deck",
            "rooms": [
                {"name": "Bridge", "use": "command", "size": "medium"},
                {"name": "Engine Room", "use": "engineering", "size": "large"},
                {"name": "Forward Hold", "use": "cargo", "size": "large"},
                {"name": "Aft Hold", "use": "cargo", "size": "large"},
                {"name": "Galley", "use": "dining", "size": "medium"},
                {"name": "Port Airlock", "use": "airlock"},
                {"name": "Starboard Airlock", "use": "airlock"},
            ],
        },
        {
            "index": -1,
            "name": "Lower Deck",
            "rooms": [
                {"name": "Shuttle Bay", "use": "hangar", "size": "huge"},
                {"name": "Hydroponics", "use": "garden", "size": "medium"},
                {"name": "Crew Berths", "use": "quarters", "size": "medium"},
            ],
        },
    ],
}


def _marks(deck) -> dict[tuple[int, int], str]:
    return {(x, y): m for y, row in enumerate(deck.hull) for x, m in enumerate(row) if m != " "}


def test_the_hull_s_mass_stands_round_each_deck_and_never_on_it_or_before_a_window():
    for brief, place in ((BRIEF, "halcyon"), (SHIP, "lark")):
        plan = generate(parse_brief(json.dumps(brief)), place)
        for deck in plan.decks.values():
            marks = _marks(deck)
            assert marks, deck.name  # a hull round it, not bare walls on the stars
            assert len(deck.hull) == len(deck.rows)
            assert all(deck.rows[y][x] == " " for x, y in marks)  # never where it builds
            h, w = len(deck.rows), len(deck.rows[0])
            for y, row in enumerate(deck.rows):
                for x, ch in enumerate(row):
                    if ch != "W":
                        continue
                    # A viewport looks onto open space, never onto plating.
                    assert any(
                        not (0 <= x + dx < w and 0 <= y + dy < h)
                        or (deck.rows[y + dy][x + dx] == " " and (x + dx, y + dy) not in marks)
                        for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0))
                    ), (deck.name, x, y)


def test_a_ship_has_its_bridge_at_the_bow_and_its_engines_at_the_stern():
    brief = parse_brief(json.dumps(SHIP))
    assert brief.hull == "ship"
    plan = generate(brief, "lark")
    main = plan.decks[0]
    bridge = next(r for r in main.rooms if r.use == "command")
    built_x = [x for row in main.rows for x, ch in enumerate(row) if ch != " "]
    assert bridge.box[2] + 1 == max(built_x)  # nothing further forward than the bridge
    assert bridge.out == (1, 0) and "W" in "".join(r[bridge.box[2] + 1] for r in main.rows)
    engines = [x for (x, _y), m in _marks(main).items() if m == "E"]
    assert engines and max(engines) < min(built_x)  # out of the stern
    engine_room = next(r for r in main.rooms if r.use == "engineering")
    assert engine_room.box[0] < bridge.box[0]
    # Airlocks off the spine itself, one each side, opening onto space.
    locks = [r for r in main.rooms if r.use == "airlock"]
    assert len(locks) == 2 and {r.out for r in locks} == {(0, -1), (0, 1)}
    port = next(r for r in locks if r.name == "Port Airlock")
    assert port.out == (0, -1)  # facing the bow, port is to the north
    # Rooms both sides of the spine, not all on one.
    cy = plan.lifts[0][1]
    sides = {r.box[1] < cy for r in main.rooms if r.use not in ("airlock", "command")}
    assert sides == {True, False}


def test_an_outpost_dug_into_an_asteroid_is_set_in_rock():
    plan = generate(parse_brief(json.dumps({**BRIEF, "shape": "cross"})), "carver", hull="rock")
    assert plan.hull == "rock"
    for deck in plan.decks.values():
        assert set(_marks(deck).values()) == {"R"}
    rock = WorldPlace(
        id="rock",
        name="Carver's Rock",
        kind="site",
        what="a small mining outpost dug into an asteroid in the belt",
    )
    from sealedlore.engine.world_art import deck_hull

    assert deck_hull(WorldMap(places={"rock": rock}), rock) == "rock"
    world, _art = _station()
    assert deck_hull(world, world.places["st"]) == ""  # the brief says


def _enclosed(deck) -> list[set[tuple[int, int]]]:
    """The patches of open space inside a deck: no hull, nothing built, and
    not reached from the frame's edge."""
    marks = _marks(deck)
    h, w = len(deck.rows), len(deck.rows[0])
    space = {
        (x, y) for y in range(h) for x in range(w) if deck.rows[y][x] == " " and (x, y) not in marks
    }
    patches: list[set[tuple[int, int]]] = []
    seen: set[tuple[int, int]] = set()
    for start in sorted(space):
        if start in seen:
            continue
        patch, todo, edge = set(), [start], False
        seen.add(start)
        while todo:
            x, y = todo.pop()
            patch.add((x, y))
            edge = edge or x in (0, w - 1) or y in (0, h - 1)
            for t in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if t in space and t not in seen:
                    seen.add(t)
                    todo.append(t)
        if not edge:
            patches.append(patch)
    return patches


def test_a_ring_keeps_one_open_middle_and_a_block_is_rooms_all_through():
    # The author (Oct 17 2026): courtyards of stars between a ring's spokes
    # read as rooms of space. A ring is rooms round one hole, its lifts on
    # the ring; any other station has no space inside at all.
    ring = generate(parse_brief(json.dumps(BRIEF)), "halcyon")
    holes = _enclosed(ring.decks[0])
    assert len(holes) == 1 and len(holes[0]) >= 100
    middle_y = sum(y for _, y in holes[0]) / len(holes[0])
    assert all(ly > middle_y for _lx, ly in ring.lifts)  # the lobby stands on the ring
    block = generate(parse_brief(json.dumps({**BRIEF, "shape": "block"})), "halcyon")
    service = [r for d in block.decks.values() for r in d.rooms if r.filler]
    assert service and {r.use for r in service} == {"storage"}
    others = {r.name.lower() for d in block.decks.values() for r in d.rooms if not r.filler}
    names = [r.name.lower() for r in service]
    assert len(names) == len(set(names)) and not others & set(names)  # each name once
    for deck in block.decks.values():
        assert not _enclosed(deck), deck.name
        reached = _reachable(deck.rows, block.lifts[0])
        for room in deck.rooms:
            assert (room.box[0], room.box[1]) in reached, (deck.name, room.name)
            assert deck.rows[room.door[1]][room.door[0]] == "+"
    assert parse_brief('{"decks": [{"index": 0, "rooms": [{"name": "Bar"}]}]}').shape == "block"


def test_set_pieces_stand_in_their_rooms_and_the_furniture_keeps_clear_of_them():
    plan = generate(parse_brief(json.dumps(SHIP)), "lark")
    pieces = {
        r.use: [(p.name, p.piece) for p in r.pieces] for d in plan.decks.values() for r in d.rooms
    }
    assert ("the reactor core", "reactor") in pieces["engineering"]
    assert all(kind == "beds" for _n, kind in pieces["garden"]) and pieces["garden"]
    assert [n for n, _k in pieces["hangar"]] == ["a small shuttlecraft"]
    assert any(n == "a stout captain's chair" for n, _k in pieces["command"])
    for deck in plan.decks.values():
        for room in deck.rooms:
            x0, y0, x1, y1 = room.box
            for p in room.pieces:
                px0, py0, px1, py1 = p.box
                assert x0 <= px0 <= px1 <= x1 and y0 <= py0 <= py1 <= y1, (room.name, p.name)
    # In the area they are placements the furniture is placed round.
    world, art = _station()
    job = ArtJob("layout:st", "layout", [], "st", extra={"deck": True, "named": []})
    ring = {**BRIEF}
    ring["decks"] = [*BRIEF["decks"]]
    ring["decks"][0] = {
        **BRIEF["decks"][0],
        "rooms": [*BRIEF["decks"][0]["rooms"], {"name": "Main Engineering", "use": "engineering"}],
    }
    art.layouts["st"] = {"": finish(job, json.dumps(ring), model="m", made_at="1")}
    area = build_area(world, art, "st")
    reactor = next(p for p in area.placements if p.piece == "reactor")
    room = area.rooms[reactor.room]
    assert "the reactor core" in room.objects  # the furnisher is told it is there
    assert area.hull and len(area.hull) == len(area.rows)
    from sealedlore.engine.area import furnish
    from sealedlore.models.area import FurnishItem, FurnishPlan, FurnishRoom

    plan_ = FurnishPlan(
        place_id="st",
        rooms=[
            FurnishRoom(
                room=room.index,
                purpose="engineering",
                items=[FurnishItem(name="a workbench", w=2, h=1, where="middle", count=6)],
            )
        ],
    )
    placed = furnish(area, plan_, "full")
    assert placed and not set(reactor.tiles()) & {t for f in placed for t in f.tiles()}


def test_the_station_set_has_its_set_pieces_and_its_hull():
    """The shipped station set has a sprite for every set piece the deck plan
    places from sprites, and the hull's and an asteroid's textures."""
    from sealedlore.engine.deck_plan import CHAIR, CONSOLE, HOLO, SHUTTLE
    from sealedlore.engine.tile_sets import item_slugs
    from sealedlore.storage.tile_sets import asset_names, read_asset, sets_folder

    folder = sets_folder()
    assert folder is not None
    station = folder / "station"
    items = {name.removeprefix("items/") for name in asset_names(station, "items")}
    for name in (CONSOLE, CHAIR, SHUTTLE, HOLO):
        assert set(item_slugs(name)) & items, name
    assert read_asset(station, "hull") and read_asset(station, "rock")
    # Nothing a station hasn't: the old set's monitor and wooden furniture.
    words = json.loads((station / "tileset.json").read_text())["words"]
    assert "glowing screen" not in words["decor0"] or "flat" in words["decor0"]
