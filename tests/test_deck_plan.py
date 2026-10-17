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
    # The ring's courtyards stay space: the drawn-plan filler leaves them be.
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
