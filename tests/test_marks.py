"""Marked spots (engine/marks.py, engine/session_area.py): a point the author
clicked on the map and put in their turn as ⌖(east, north). The storyteller
is told what is there; the read may put a new place or a person on it, and
the program puts them exactly there."""

from __future__ import annotations

import json

from sealedlore.engine.area import point_tile, tile_point
from sealedlore.engine.marks import mark_token, marks_in, place_at
from tests.test_area import make_session, turn, world_and_art


def test_a_mark_is_its_coordinate_and_lies_in_the_innermost_place_there():
    assert mark_token(412.4, -88.6) == "⌖(412, -89)"
    found = marks_in("Go to ⌖(412, -89), then @(3,4) and ⌖(412, -89) again.")
    assert [(m.x, m.y) for m in found] == [(412.0, -89.0), (3.0, 4.0)]
    world, art = world_and_art()
    assert place_at(world, art, 20.0, 30.0) == "house"  # the house, not the farm round it
    assert place_at(world, art, -150.0, 50.0) == "farm"
    assert place_at(world, art, 3000.0, 3000.0) is None  # open country


def test_a_tile_is_a_point_on_the_map_and_back(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    world = session.world()
    farm = session.area("farm")
    for tile in ((0, 0), (17, 40), (farm.width - 1, farm.height - 1)):
        point = tile_point(world, farm, *tile)
        assert point_tile(world, farm, *point) == tile
    # The farm is 400 m wide, its plan centred on its spot.
    west = tile_point(world, farm, 0, 0)
    assert -200 < west[0] < -195 and 115 < west[1] < 120


def test_the_storyteller_and_the_read_are_told_what_a_marked_spot_is(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["A crow lands there.", "{}"])
    session.config.scene_reads = "manual"
    well = next(p for p in session.area("farm").placements if "well" in p.name)
    token = session.mark_of_tile("farm", well.x, well.y + 1)
    list(session.send(turn(f"I point at {token}.")))
    user = session.path()[-2]
    marks = user.meta.area.marks
    assert len(marks) == 1 and marks[0].place_id == "farm" and marks[0].spot.area == "farm"
    assert "in The farm" in marks[0].words and "by the stone well" in marks[0].words
    assert "of Serrik Vaun" in marks[0].words  # how far, and which way
    sent = session.provider.requests[0].messages[-1].text
    assert "The author marked these spots on the map" in sent and f"- {token}: " in sent
    read = session.provider.requests[-1].messages[-1].text
    assert "SPOTS THE AUTHOR MARKED ON THE MAP IN THEIR TURN:" in read and token in read
    # The passage doesn't carry the turn's marks on.
    assert session.path()[-1].meta.area.marks == []


def test_a_place_made_at_a_mark_is_put_there_and_someone_sent_there_stands_on_it(
    tmp_path, story, cast
):
    session = make_session(tmp_path, story, cast)
    token = session.mark_of_tile("farm", 30, 50)
    point = marks_in(token)[0]
    read = json.dumps(
        {
            "places": [
                {
                    "name": "The rift",
                    "inside": "Hellsville",  # wrong: the mark says where it is
                    "kind": "site",
                    "mark": token,
                    "quote": "A rift opens",
                }
            ],
            "links": [
                {"from": "The rift", "to": "Hellsville", "direction": "north", "quote": "A rift"}
            ],
            "where": [{"who": "Serrik", "place": "The rift", "quote": "Serrik walks to it"}],
            "at": {"Serrik": token},
        }
    )
    session.provider.responses = ["A rift opens. Serrik walks to it.", read]
    session.config.scene_reads = "manual"
    list(session.send(turn(f"Direction: a rift opens at {token}.")))
    world = session.world()
    rift = next(p for p in world.places.values() if p.name == "The rift")
    assert (rift.x, rift.y) == (point.x, point.y) and rift.inside == "farm"
    assert world.where["Serrik Vaun"] == rift.id
    me = session.path()[-1].meta.area.spots["Serrik Vaun"]
    assert me.area == "farm" and abs(me.x - 30) + abs(me.y - 50) <= 2


def test_going_to_a_spot_in_another_area_travels_there(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    point = tile_point(session.world(), session.area("farm"), 30, 50)
    text = session.go_to_point(*point)
    assert text == f"I make my way to {mark_token(*point)}, in The farm."
    state = session.area_state()
    assert state.spots["Serrik Vaun"].area == "house"  # not until the turn is sent
    assert state.action.verb == "travel" and state.action.seconds > 0
    list(session.send(turn(text)))
    me = session.area_state().spots["Serrik Vaun"]
    assert me.area == "farm" and abs(me.x - 30) + abs(me.y - 50) <= 2
    # In the area already: walking is the way, and nothing is written.
    assert session.go_to_point(*tile_point(session.world(), session.area("farm"), 31, 50)) == ""


def test_a_read_that_gives_a_spot_s_words_for_its_coordinate_still_puts_it_there(
    tmp_path, story, cast
):
    # Live (Sonnet 4.6 telling, DeepSeek V4.1 Flash reading): "mark": "in the
    # kitchen and eating area", the spot's words, not its coordinate.
    session = make_session(tmp_path, story, cast)
    token = session.mark_of_tile("farm", 30, 50)
    point = marks_in(token)[0]
    read = json.dumps(
        {
            "places": [
                {
                    "name": "The shrine",
                    "inside": "The farm",
                    "kind": "site",
                    "mark": "in The farm",
                    "quote": "A shrine stands",
                }
            ]
        }
    )
    session.provider.responses = ["A shrine stands there.", read]
    session.config.scene_reads = "manual"
    list(session.send(turn(f"A shrine stands at {token}.")))
    shrine = next(p for p in session.world().places.values() if p.name == "The shrine")
    assert (shrine.x, shrine.y) == (point.x, point.y)
    sent = session.provider.requests[-1].messages[-1].text
    assert f'"mark": "{token}"' in sent  # the coordinate to copy, shown


def test_a_retake_is_told_the_turn_s_own_positions_and_marks(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["One.", "{}", "Two.", "{}", "Three.", "{}"])
    session.config.scene_reads = "manual"
    house = session.area("house")
    door = next(e for e in house.exits if e[1] == 23)
    session.walked(session.walk_route(*door))
    session.go_through()
    token = session.mark_of_tile("farm", 30, 50)
    list(session.send(turn(f"I point at {token}.")))
    first = session.provider.requests[-2].messages[-1].text
    assert "- Serrik Vaun: in The farm" in first and f"- {token}: " in first
    # Walk on after the passage, then retake: the retake gets the turn's
    # own snapshot and marks, not the walking done since (the review).
    session.walked(session.walk_route(40, 50))
    list(session.regenerate())
    retake = session.provider.requests[-2].messages[-1].text
    block = lambda t: t[t.index("Where everyone stands") :].split("\n\n")[0]  # noqa: E731
    assert block(retake) == block(first) and f"- {token}: " in retake
    # A Question carries neither a pending menu choice nor the live walk.
    session.map_action("talk", "Maela Orr")
    list(session.ask("Why is it cold?"))
    asked = session.provider.requests[-1].messages[-1].text
    assert "The author chose" not in asked and "Since the last passage" not in asked


def test_an_edited_turn_s_marks_are_its_new_text_s(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["One.", "{}"])
    session.config.scene_reads = "manual"
    list(session.send(turn("I look around.")))
    user = session.path()[-2]
    token = session.mark_of_tile("farm", 30, 50)
    session.edit_node(user.id, f"I point at {token}.")
    assert [m.token for m in user.meta.area.marks] == [token]
    session.edit_node(user.id, "I look around again.")
    assert user.meta.area.marks == []


def test_a_named_spot_under_at_is_never_taken_for_the_one_mark(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    token = session.mark_of_tile("farm", 30, 50)
    read = json.dumps(
        {
            "where": [{"who": "Maela", "place": "The house", "quote": "Maela came in"}],
            "at": {"Maela": "the cast-iron stove"},
            "places": [
                {
                    "name": "The cairn",
                    "inside": "The farm",
                    "kind": "site",
                    "mark": "none",
                    "quote": "a cairn",
                }
            ],
        }
    )
    session.provider.responses = ["Maela came in. There is a cairn.", read]
    session.config.scene_reads = "manual"
    list(session.send(turn(f"I glance at {token}.")))
    world = session.world()
    assert world.where["Maela Orr"] == "house"  # the stove, in the house, not the mark
    cairn = next(p for p in world.places.values() if p.name == "The cairn")
    point = marks_in(token)[0]
    assert (cairn.x, cairn.y) != (point.x, point.y)


def test_a_place_at_a_mark_inside_another_new_place_keeps_its_inside(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast)
    a = session.mark_of_tile("farm", 30, 50)
    b = session.mark_of_tile("farm", 32, 50)
    read = json.dumps(
        {
            "places": [
                {
                    "name": "The camp",
                    "inside": "The farm",
                    "kind": "site",
                    "mark": a,
                    "size": 30,
                    "quote": "a camp",
                },
                {
                    "name": "The tent",
                    "inside": "The camp",
                    "kind": "building",
                    "mark": b,
                    "quote": "a tent",
                },
            ]
        }
    )
    session.provider.responses = ["There is a camp, with a tent.", read]
    session.config.scene_reads = "manual"
    list(session.send(turn(f"Direction: a camp at {a}, its tent at {b}.")))
    world = session.world()
    camp = next(p for p in world.places.values() if p.name == "The camp")
    tent = next(p for p in world.places.values() if p.name == "The tent")
    assert tent.inside == camp.id and (tent.x, tent.y) == (marks_in(b)[0].x, marks_in(b)[0].y)


def test_a_mark_on_a_known_place_moves_it_only_on_the_author_s_direction(tmp_path, story, cast):
    """Placed once: in character, a read that puts a known place at the
    turn's mark is refused and logged; the author's Direction moves it
    (tests/test_director_map.py)."""
    from sealedlore.storage.repository import read_api_log

    session = make_session(tmp_path, story, cast)
    token = session.mark_of_tile("farm", 100, 50)
    read = json.dumps(
        {"places": [{"name": "The house", "mark": token, "quote": "The house stood there"}]}
    )
    session.provider.responses = ["The house stood there.", read]
    session.config.scene_reads = "manual"
    list(session.send(turn(f"I walk to the house, at {token}.")))
    house = session.world().places["house"]
    assert (house.x, house.y) == (20.0, 30.0)
    refused = [
        e for e in read_api_log(session.story.id, root=tmp_path) if e["kind"] == "world_refused"
    ]
    assert (
        refused
        and "only the author's Direction or Narration moves a place" in refused[0]["refused"][0]
    )


def test_a_mark_on_a_docked_ship_is_on_the_ship():
    from sealedlore.models.world import WorldPlace

    world, art = world_and_art()
    ship = WorldPlace(id="ship", name="The Wren", kind="vehicle", inside="town", size=25.0)
    world.places["ship"] = ship
    assert place_at(world, art, 0.0, 8000.0) == "ship"  # where the town is, the ship is docked
