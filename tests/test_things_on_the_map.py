"""Every thing on the play view, whoever put it there (the author, Oct 17
2026: "Things the story adds must show on the play view, that keeps coming
up and MUST be fixed. Furniture laid by the program should also be no
different to any other kind, it should be movable."): the map's things are
laid on the area they are in, the read says what a passage did to any thing
there, and a record keeps it (engine/area.py `lay_things`, `thing_tile`,
`apply_things`; engine/session_area.py `_things_after_read`)."""

from __future__ import annotations

import json
import math

from sealedlore.engine.area import thing_tile
from sealedlore.models.area import LiveArea, Spot
from sealedlore.models.world import WorldThing
from tests.test_area import make_session, turn

ANVIL = WorldThing(id="anvil", name="a heavy iron anvil", at="house")
TABLE = {"kind": "object", "name": "a heavy oak table", "x": 8, "y": 10}
SHELF = {"kind": "object", "name": "a tall bookshelf", "x": 30, "y": 12}


def placed(session, area: str, part: str):
    return next((p for p in session.area(area).placements if part in p.name), None)


def stand(session, x: int, y: int, area: str = "house") -> None:
    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area=area, x=x, y=y)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)


def read(*things: dict, **more) -> str:
    return json.dumps({"things": list(things), **more})


def test_the_map_s_things_are_laid_on_the_area_they_are_in(tmp_path, story, cast):
    rifle = WorldThing(id="rifle", name="an old rifle", at="farm")
    session = make_session(tmp_path, story, cast, things=[ANVIL, rifle])
    anvil = placed(session, "house", "anvil")
    assert anvil is not None and anvil.id == "anvil"
    assert "use" in anvil.verbs and "take" not in anvil.verbs  # too heavy to carry
    house = session.area("house")
    assert house.walkable(anvil.x, anvil.y)  # on floor, the tile its own
    assert (anvil.x, anvil.y) in house.blocked()
    assert placed(session, "house", "rifle") is None  # it is at the farm
    rifle_here = placed(session, "farm", "rifle")
    assert rifle_here is not None and "take" in rifle_here.verbs


def test_a_thing_the_area_draws_already_is_not_laid_twice(tmp_path, story, cast):
    lantern = {"kind": "object", "name": "a brass lantern", "x": 28, "y": 5}
    thing = WorldThing(id="lantern", name="A brass lantern", at="house")
    session = make_session(tmp_path, story, cast, house_objects=[lantern], things=[thing])
    found = [p for p in session.area("house").placements if "lantern" in p.name]
    assert len(found) == 1 and found[0].id != "lantern"  # the plan's own


def test_a_thing_the_story_brings_lies_beside_the_author_s_character(tmp_path, story, cast):
    passage = "A heavy iron anvil stands a few steps from Serrik, left by the last smith."
    reply = read(
        {"name": "a heavy iron anvil", "at": "The house", "quote": "A heavy iron anvil stands"}
    )
    session = make_session(tmp_path, story, cast, ["Quiet.", "{}", passage, reply])
    list(session.send(turn("I look around.")))
    me = session.area_state().spots["Serrik Vaun"]
    list(session.send(turn("I wait.")))
    record = next(r for r in session.path()[-1].meta.area.things.values() if "anvil" in r.name)
    assert record.state == "dropped" and record.spot.area == "house"
    assert abs(record.spot.x - me.x) + abs(record.spot.y - me.y) <= 4
    anvil = placed(session, "house", "anvil")
    assert (anvil.x, anvil.y) == (record.spot.x, record.spot.y)


def test_the_read_is_told_the_things_on_the_play_view(tmp_path, story, cast):
    session = make_session(
        tmp_path, story, cast, ["Quiet.", "{}", "Still.", "{}"], house_objects=[TABLE]
    )
    list(session.send(turn("I look around.")))
    list(session.send(turn("I wait.")))
    sent = session.provider.requests[-1].messages[-1].text
    line = sent[sent.index("THINGS ON THE PLAY VIEW") :]
    assert "the heavy oak table" in line and "the cast-iron stove" in line
    assert "furniture included" in line


def test_the_read_moves_a_movable_thing_to_a_spot_and_never_a_fixed_one(tmp_path, story, cast):
    passage = "Serrik shoves the heavy oak table across to the stove. The stove won't shift."
    reply = read(
        {
            "name": "heavy oak table",
            "spot": "the cast-iron stove",
            "quote": "Serrik shoves the heavy oak table across to the stove.",
        },
        {"name": "cast-iron stove", "spot": "the heavy oak table", "quote": "The stove"},
    )
    session = make_session(
        tmp_path, story, cast, ["Quiet.", "{}", passage, reply], house_objects=[TABLE]
    )
    list(session.send(turn("I look around.")))
    table = placed(session, "house", "oak table")
    stove = placed(session, "house", "stove")
    list(session.send(turn("I shove the table over to the stove.")))
    moved = placed(session, "house", "oak table")
    assert moved.id == table.id and (moved.w, moved.h) == (table.w, table.h)
    assert math.hypot(moved.x - stove.x, moved.y - stove.y) < math.hypot(
        table.x - stove.x, table.y - stove.y
    )
    record = session.path()[-1].meta.area.things[table.id]
    assert record.state == "dropped" and record.spot.area == "house"
    assert (placed(session, "house", "stove").x, placed(session, "house", "stove").y) == (
        stove.x,
        stove.y,
    )
    refused = [
        e
        for e in map(json.loads, (tmp_path / "stories" / story.id / "api_log.jsonl").open())
        if e["kind"] == "world_refused"
    ]
    assert refused and "cast-iron stove" in refused[-1]["refused"][-1]
    # The map's own list is left alone: the table is the play view's.
    assert not any("table" in t.name for t in session.world().things.values())


def test_the_read_s_take_break_and_use_up_are_kept(tmp_path, story, cast):
    lantern = {"kind": "object", "name": "a brass lantern", "x": 28, "y": 5}
    bread = {"kind": "object", "name": "a loaf of bread", "x": 30, "y": 5}
    passage = "Maela pockets the lantern. Serrik eats the bread and kicks the table to pieces."
    reply = read(
        {"name": "brass lantern", "at": "carried by Maela", "quote": "Maela pockets the lantern."},
        {"name": "loaf of bread", "state": "gone", "quote": "Serrik eats the bread"},
        {"name": "heavy oak table", "state": "broken", "quote": "kicks the table to pieces"},
    )
    session = make_session(
        tmp_path,
        story,
        cast,
        ["Quiet.", "{}", passage, reply],
        house_objects=[lantern, bread, TABLE],
    )
    list(session.send(turn("I look around.")))
    list(session.send(turn("I wait.")))
    assert placed(session, "house", "lantern") is None and placed(session, "house", "bread") is None
    assert placed(session, "house", "oak table").broken
    assert session.carried_things("Maela Orr")[0][1] == "brass lantern"


def test_a_heavy_thing_carried_off_goes_beside_its_carrier(tmp_path, story, cast):
    passage = "Maela hauls the bookshelf over to where she stands."
    reply = read(
        {"name": "tall bookshelf", "at": "carried by Maela", "quote": "Maela hauls the bookshelf"}
    )
    session = make_session(
        tmp_path, story, cast, ["Quiet.", "{}", passage, reply], house_objects=[SHELF]
    )
    list(session.send(turn("I look around.")))
    list(session.send(turn("I wait.")))
    shelf = placed(session, "house", "bookshelf")
    maela = session.area_state().spots["Maela Orr"]
    assert maela.area == "house" and math.hypot(shelf.x - maela.x, shelf.y - maela.y) <= 4
    assert session.carried_things("Maela Orr") == []  # not carried: too heavy


def test_a_thing_lies_clear_of_doorways_and_cuts_no_ground_off(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    house = session.area("house")
    door = next(e for e in house.exits if e[1] == 23)
    x, y = thing_tile(house, (door[0], door[1] - 1))
    assert (x, y) != door and abs(x - door[0]) + abs(y - door[1]) > 1
    assert house.walkable(x, y) and (x, y) not in house.blocked()


def test_a_thing_the_author_puts_at_a_marked_spot_lies_on_it(tmp_path, story, cast):
    from sealedlore.engine.marks import marks_in

    session = make_session(tmp_path, story, cast)
    token = session.mark_of_tile("farm", 30, 50)
    assert marks_in(token)
    reply = read({"name": "a large boulder", "at": token, "quote": "A large boulder"})
    session.provider.responses = ["A large boulder crashes down in the yard.", reply]
    session.config.scene_reads = "manual"
    list(session.send(turn(f"Direction: a large boulder falls at {token}.")))
    boulder = placed(session, "farm", "boulder")
    assert boulder is not None and abs(boulder.x - 30) + abs(boulder.y - 50) <= 2
    assert "take" not in boulder.verbs  # movable, with strength; never carried off


def test_a_short_name_is_the_thing_it_shortens_never_the_other_way():
    from sealedlore.engine.takeable import same_thing

    assert same_thing("heavy iron anvil", "heavy iron anvil block on a timber stand")
    assert same_thing("Second workbench", "Second workbench strewn with tiny brass cogs")
    # The sample map's "Workbench" is not the plan's second workbench.
    assert not same_thing("Second workbench strewn with tiny brass cogs", "Workbench")
    assert not same_thing("the", "a heavy anvil")  # says nothing of what it is


def test_where_a_thing_went_is_read_from_its_state_when_no_spot_is_given(tmp_path, story, cast):
    from sealedlore.engine.area import spot_in_words

    session = make_session(tmp_path, story, cast, house_objects=[TABLE])
    house = session.area("house")
    near = (26, 5)
    assert spot_in_words(house, "dragged to the door", near) == "the doorway"
    assert spot_in_words(house, "shoved across the doorway, blocking it", near) == "the doorway"
    assert spot_in_words(house, "moved next to the cast-iron stove", near) == "the cast-iron stove"
    assert spot_in_words(house, "battered and scratched", near) == ""  # says where it went? no
    assert spot_in_words(house, "", near) == ""


def test_a_move_the_read_puts_in_the_state_is_kept(tmp_path, story, cast):
    passage = "Serrik drags the heavy oak table to the door and wedges it there."
    reply = read(
        {
            "name": "heavy oak table",
            "at": "The house",
            "state": "dragged to the door, blocking it",
            "quote": "Serrik drags the heavy oak table to the door",
        }
    )
    session = make_session(
        tmp_path, story, cast, ["Quiet.", "{}", passage, reply], house_objects=[TABLE]
    )
    list(session.send(turn("I look around.")))
    table = placed(session, "house", "oak table")
    list(session.send(turn("I drag the table to the door.")))
    moved = placed(session, "house", "oak table")
    house = session.area("house")
    door = min(
        (d.middle for d in house.doorways),
        key=lambda m: math.hypot(m[0] - moved.x, m[1] - moved.y),
    )
    assert (moved.x, moved.y) != (table.x, table.y)
    assert math.hypot(moved.x - door[0], moved.y - door[1]) <= 2  # at the door, blocking it


def test_a_map_thing_the_plan_draws_under_a_longer_name_is_drawn_once(tmp_path, story, cast):
    """Live (the sample's barn): the map's "Pickup truck" and the plan's
    "Pickup truck (rusted, flat tyres, no fuel)" were both drawn."""
    truck = {"kind": "object", "name": "Pickup truck (rusted, flat tyres)", "x": 30, "y": 12}
    thing = WorldThing(id="truck", name="Pickup truck", at="house")
    session = make_session(tmp_path, story, cast, house_objects=[truck], things=[thing])
    found = [p for p in session.area("house").placements if "truck" in p.name.lower()]
    assert len(found) == 1 and found[0].id != "truck"


def test_a_heavy_thing_said_carried_off_is_dragged_beside_its_carrier(tmp_path, story, cast):
    """The replay (Oct 17 2026): the read wrote an anvil being dragged as
    "carried by" its dragger, and it was put in her hands."""
    passage = "Maela drags the anvil across the room to where she stands by the far wall."
    reply = read(
        {
            "name": "heavy iron anvil",
            "at": "carried by Maela",
            "state": "being dragged",
            "quote": "Maela drags the anvil across the room",
        }
    )
    session = make_session(tmp_path, story, cast, ["Quiet.", "{}", passage, reply], things=[ANVIL])
    list(session.send(turn("I look around.")))
    state = session.area_state()
    state.spots["Maela Orr"] = Spot(area="house", x=35, y=18)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)
    list(session.send(turn("Maela, drag that anvil over to you.")))
    anvil = placed(session, "house", "anvil")
    maela = session.area_state().spots["Maela Orr"]
    assert anvil is not None and math.hypot(anvil.x - maela.x, anvil.y - maela.y) <= 3
    assert session.carried_things("Maela Orr") == []
    assert session.world().things["anvil"].carried_by is None
