"""At a building but outside it (Oct 17 2026): live, John knocking on the
farmhouse door was stood in its kitchen, and the next passage was told so.
The read marks "outside"; the play view stands them at its door on the ground
round it."""

from __future__ import annotations

import json

from sealedlore.engine.world import applied, render_map_context, resolve_reply
from tests.test_area import make_session, turn, world_and_art

KNOCK = "Serrik steps up onto the porch and knocks."


def at_house(outside: bool | None, quote: str = KNOCK) -> dict:
    entry = {"who": "Serrik Vaun", "place": "The house", "quote": quote}
    if outside is not None:
        entry["outside"] = outside
    return {"where": [entry]}


def test_the_read_s_outside_is_kept_until_a_placement_without_it():
    world, _art = world_and_art()
    delta, refused = resolve_reply(world, at_house(True), source=KNOCK, people=[])
    assert refused == [] and delta.outside == {"Serrik Vaun": True}
    world = applied(world, delta)
    assert world.outside == ["Serrik Vaun"]
    assert "is at: just outside The house, in The farm" in render_map_context(world, "Serrik Vaun")
    inside = "Serrik goes in."
    delta, _ = resolve_reply(world, at_house(None, inside), source=inside, people=[])
    world = applied(world, delta)
    assert world.outside == []


def test_outside_a_building_stands_them_at_its_door_and_the_read_is_told_last(
    tmp_path, story, cast
):
    session = make_session(
        tmp_path,
        story,
        cast,
        [
            KNOCK,
            json.dumps(at_house(True)),
            "Serrik goes in.",
            json.dumps(at_house(None, "goes in")),
        ],
    )
    session.config.scene_reads = "manual"
    list(session.send(turn("I knock.")))
    asked = session.provider.requests[-1].messages[-1].text
    assert asked.rstrip().endswith(session.texts["map_read.outside"])
    me = session.path()[-1].meta.area.spots["Serrik Vaun"]
    assert me.area == "farm"  # on the yard, not in the kitchen
    door = next(e for e in session.area("farm").entrances if e.place_id == "house")
    assert abs(me.x - door.x) + abs(me.y - door.y) <= 3
    list(session.send(turn("I go in.")))
    assert session.path()[-1].meta.area.spots["Serrik Vaun"].area == "house"


def test_walking_in_from_the_door_puts_them_in(tmp_path, story, cast):
    session = make_session(
        tmp_path, story, cast, [KNOCK, json.dumps(at_house(True)), "Warm.", "{}"]
    )
    session.config.scene_reads = "manual"
    list(session.send(turn("I knock.")))
    assert "Serrik Vaun" in session.world().outside
    door = next(e for e in session.area("farm").entrances if e.place_id == "house")
    session.walked(session.walk_route(door.x, door.y))
    if session.door_here() is not None:
        session.go_through()
    assert session.area_state().spots["Serrik Vaun"].area == "house"
    list(session.send(turn("I warm my hands.")))
    assert "Serrik Vaun" not in session.world().outside
