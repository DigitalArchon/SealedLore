"""The author's Direction or Narration changes the map broadly (engine/world.py
`resolve_reply`, `applied`; engine/session_world.py). The author, Oct 16
2026: "The Director must be able to make as broad and complete changes as
possible." Places are placed once and never moved, except as the Director
says: a place taken off the map, renamed, resized, moved (to a spot the
author marked, or by a way the turn states) or put inside another. Each
stands on a quote, and on an in-character turn each is refused and logged.
"""

from __future__ import annotations

import json

from sealedlore.engine.marks import marks_in
from sealedlore.engine.prompt import TurnRequest
from sealedlore.engine.session import SessionNotice
from sealedlore.engine.world import (
    applied,
    describe_delta,
    find_place,
    place_delta,
    render_map,
    resolve_reply,
)
from sealedlore.engine.world_art import fixed_in_plan, with_fixed
from sealedlore.models.character import Character
from sealedlore.models.node import DIRECTOR_SPEAKER_ID
from sealedlore.models.world import WorldDelta, WorldLink, WorldMap, WorldPlace, WorldThing
from sealedlore.storage.repository import read_api_log
from tests.test_area import make_session, turn

DIRECTION = (
    "The barn is gone. The house is now called Hearth House. The farm is now 800 metres "
    "across. The barn now stands one mile east of Hellsville. The house is now inside "
    "Hellsville. The house now stands 300 metres north of the farm. The house is 50 metres "
    "north of the farm."
)
PASSAGE = "Nothing stirred in the snow."
SOURCE = f"{DIRECTION}\n\n{PASSAGE}"
PEOPLE = [
    Character(id="c-jane", name="Jane"),
    Character(id="c-marcus", name="Marcus Webb"),
    Character(id="c-ben", name="Ben"),
]
ONLY = "only the author's Direction or Narration"


def estate() -> WorldMap:
    """A farm with a house and a barn (a loft in it), a town five miles north;
    Jane in the barn, Marcus in the house, Ben on the farm; an axe in the barn."""
    farm = WorldPlace(id="farm", name="The farm", kind="site", x=0.0, y=0.0, size=400.0)
    house = WorldPlace(id="house", name="The house", kind="building", inside="farm", x=0.0, y=50.0)
    barn = WorldPlace(id="barn", name="The barn", kind="building", inside="farm", x=120.0, y=-40.0)
    loft = WorldPlace(id="loft", name="The loft", kind="room", inside="barn", x=122.0, y=-38.0)
    town = WorldPlace(id="town", name="Hellsville", kind="settlement", x=0.0, y=8000.0, size=1500.0)
    return WorldMap(
        places={p.id: p for p in (farm, house, barn, loft, town)},
        links=[
            WorldLink(a="farm", b="town", direction="north", distance="five miles"),
            WorldLink(a="barn", b="house", direction="north", distance="a few steps"),
        ],
        where={"Jane": "barn", "Marcus Webb": "house", "Ben": "farm"},
        visited={"Jane": ["barn"], "Marcus Webb": ["house"], "Ben": ["farm"]},
        things={"axe": WorldThing(id="axe", name="Axe", at="barn")},
        seen=["farm", "house", "barn", "loft", "town"],
        start="barn",
    )


def resolve(world: WorldMap, reply: dict, *, says: bool, marked=None):
    return resolve_reply(
        world, reply, source=SOURCE, people=PEOPLE, author_says=says, marked=marked
    )


def unchanged(world: WorldMap, delta: WorldDelta) -> bool:
    return applied(world, delta).model_dump() == world.model_dump()


# --- each change, on the author's Direction and on an in-character turn ---------------


def test_the_direction_takes_a_place_off_the_map_and_what_was_there_goes_to_its_parent():
    world = estate()
    reply = {"gone": [{"name": "the barn", "quote": "The barn is gone"}]}
    delta, refused = resolve(world, reply, says=True)
    assert refused == [] and delta.removed == ["barn"] and not delta.empty()
    after = applied(world, delta)
    assert "barn" not in after.places
    assert after.places["loft"].inside == "farm"  # re-nested, where it stands
    assert after.where["Jane"] == "farm" and "farm" in after.visited["Jane"]
    assert after.things["axe"].at == "farm"
    assert all("barn" not in (k.a, k.b) for k in after.links) and len(after.links) == 1
    assert "barn" not in after.seen and after.start == "farm"
    # On an in-character turn: refused, logged, and the map as it was.
    delta, refused = resolve(world, reply, says=False)
    assert refused == [f"gone the barn: {ONLY} removes a place"]
    assert delta.empty() and unchanged(world, delta)
    # Taken off with no parent: whoever was there is nowhere until placed again.
    delta, _ = resolve(
        world, {"gone": [{"name": "The farm", "quote": "Nothing stirred"}]}, says=True
    )
    after = applied(world, delta)
    assert after.places["house"].inside is None and after.places["barn"].inside is None
    assert "Ben" not in after.where and after.where["Jane"] == "barn"
    # Nothing in this reply may still name it.
    delta, refused = resolve(
        world,
        {
            "gone": [{"name": "the barn", "quote": "The barn is gone"}],
            "where": [{"who": "Jane", "place": "The barn", "quote": "Nothing stirred"}],
            "links": [{"from": "The barn", "to": "Hellsville", "quote": "Nothing stirred"}],
            "things": [{"name": "Axe", "at": "The barn", "quote": "Nothing stirred"}],
        },
        says=True,
    )
    assert [r.split(":")[1].strip() for r in refused] == ["taken off the map"] * 2
    assert delta.where == {} and delta.links == [] and delta.things[0].at == "farm"


def test_the_direction_renames_a_place_and_the_old_name_stays_an_alias_on_every_fold():
    world = estate()
    reply = {
        "places": [{"name": "Hearth House", "was": "the house", "quote": "now called Hearth House"}]
    }
    delta, refused = resolve(world, reply, says=True)
    assert refused == [] and delta.renamed == {"house": "Hearth House"}
    after = applied(world, delta)
    house = after.places["house"]
    assert house.name == "Hearth House" and house.aliases == ["The house"]
    assert (
        find_place(after, "the house") == "house" and find_place(after, "hearth house") == "house"
    )
    assert (house.x, house.y, house.inside) == (0.0, 50.0, "farm")  # nothing else moved
    assert "- Hearth House (building)" in render_map(after)
    again = applied(after, delta)  # folded again: the same, no second alias
    assert again.places["house"].aliases == ["The house"]
    # The "renamed" list says the same.
    listed = {"renamed": [{"was": "The house", "now": "Hearth House", "quote": "Hearth House"}]}
    assert resolve(world, listed, says=True)[0].renamed == {"house": "Hearth House"}
    # A name another place has, and one the map doesn't know, are refused.
    delta, refused = resolve(
        world, {"places": [{"name": "The barn", "was": "the house", "quote": "called"}]}, says=True
    )
    assert refused == ["rename the house -> The barn: The barn is another place's name"]
    assert delta.renamed == {} and applied(world, delta).places["house"].name == "The house"
    _delta, refused = resolve(
        world, {"places": [{"name": "The mill", "was": "the inn", "quote": "called"}]}, says=True
    )
    assert refused == ["rename the inn -> The mill: the inn is not on the map"]
    # On an in-character turn: refused, and the entry is about the old place.
    delta, refused = resolve(world, reply, says=False)
    assert refused == [f"rename the house -> Hearth House: {ONLY} renames a place"]
    assert delta.renamed == {} and unchanged(world, delta)
    assert [p.id for p in delta.places] == ["house"]  # no second place under the new name


def test_the_direction_resizes_a_place():
    world = estate()
    reply = {"places": [{"name": "The farm", "size": 800, "quote": "now 800 metres across"}]}
    delta, refused = resolve(world, reply, says=True)
    assert refused == []
    farm = applied(world, delta).places["farm"]
    assert farm.size == 800 and (farm.x, farm.y) == (0.0, 0.0) and delta.moved == []
    delta, refused = resolve(world, reply, says=False)
    assert refused == [f"place The farm: {ONLY} resizes a place"]
    assert unchanged(world, delta)
    # Its own size again is no change, on any turn.
    same = {"places": [{"name": "The farm", "size": 400, "quote": "Nothing stirred"}]}
    assert resolve(world, same, says=False)[1] == []


def test_the_direction_moves_a_place_to_a_marked_spot_and_what_is_inside_goes_with_it():
    world = estate()
    reply = {"places": [{"name": "The barn", "mark": "⌖(300, -200)", "quote": "The barn now"}]}
    marked = {"barn": (300.0, -200.0)}
    delta, refused = resolve(world, reply, says=True, marked=marked)
    assert refused == [] and delta.moved == ["barn"]
    (copy,) = delta.places
    assert (copy.x, copy.y) == (300.0, -200.0)  # the session fits it in what is there
    after = applied(world, delta)
    assert (after.places["barn"].x, after.places["barn"].y) == (300.0, -200.0)
    assert (after.places["loft"].x, after.places["loft"].y) == (302.0, -198.0)
    assert after.where["Jane"] == "barn"  # she moved with it
    assert [(k.a, k.b) for k in after.links] == [("farm", "town")]  # its old way is gone
    # On an in-character turn the mark moves nothing: placed once.
    delta, refused = resolve(world, reply, says=False, marked=marked)
    assert refused == [f"place The barn: {ONLY} moves a place"]
    assert delta.moved == [] and unchanged(world, delta)
    # Under its old name, when renamed and moved in one entry.
    both = {
        "places": [
            {"name": "The red barn", "was": "the barn", "mark": "⌖(300, -200)", "quote": "barn"}
        ]
    }
    delta, _ = resolve(world, both, says=True, marked={"barn": (300.0, -200.0)})
    assert delta.moved == ["barn"] and delta.renamed == {"barn": "The red barn"}


def test_the_direction_moves_a_place_by_a_way_it_states():
    world = estate()
    reply = {
        "places": [{"name": "The barn", "quote": "The barn now stands one mile east"}],
        "links": [
            {
                "from": "Hellsville",
                "to": "The barn",
                "direction": "east",
                "distance": "one mile",
                "quote": "The barn now stands one mile east of Hellsville",
            }
        ],
    }
    delta, refused = resolve(world, reply, says=True)
    assert refused == [] and delta.moved == ["barn"]
    (copy,) = delta.places
    assert copy.x is None and copy.inside is None  # out of the farm: beside the town
    place_delta(world, delta, "Jane")
    assert delta.conflicts == []  # the author's say is no disagreement
    after = applied(world, delta)
    barn, loft = after.places["barn"], after.places["loft"]
    assert abs(barn.x - 1609) < 50 and abs(barn.y - 8000) < 50
    assert abs(loft.x - barn.x - 2) < 1e-6 and abs(loft.y - barn.y - 2) < 1e-6  # went with it
    assert [(k.a, k.b) for k in after.links] == [("farm", "town"), ("town", "barn")]
    # On an in-character turn the way is noted against the map, which stands.
    delta, refused = resolve(world, reply, says=False)
    assert refused == [] and delta.moved == []
    place_delta(world, delta, "Jane")
    assert delta.conflicts and "one mile" in delta.conflicts[0]
    assert applied(world, delta).places["barn"].x == 120.0
    # Measured from what it is in: outside it when the distance says so.
    out = {
        "places": [{"name": "The house", "quote": "The house now stands 300 metres north"}],
        "links": [
            {
                "from": "The farm",
                "to": "The house",
                "direction": "north",
                "distance": "300 metres",
                "quote": "The house now stands 300 metres north of the farm",
            }
        ],
    }
    delta, _ = resolve(world, out, says=True)
    assert delta.moved == ["house"] and delta.places[0].inside is None
    house = applied(world, delta).places["house"]
    assert house.inside is None and abs(house.x) < 1e-6 and abs(house.y - 300) < 1e-6
    within = json.loads(json.dumps(out).replace("300 metres", "50 metres"))
    delta, _ = resolve(world, within, says=True)
    assert delta.moved == [] and unchanged(world, delta)  # still in the farm: no move
    # A way with no direction or no distance the layout can read moves nothing.
    vague = json.loads(json.dumps(reply).replace('"east"', '"beyond"'))
    assert resolve(world, vague, says=True)[0].moved == []


def test_the_direction_puts_a_place_inside_another():
    world = estate()
    reply = {
        "places": [{"name": "The house", "inside": "Hellsville", "quote": "inside Hellsville"}]
    }
    delta, refused = resolve(world, reply, says=True)
    assert refused == [] and delta.moved == ["house"]
    (copy,) = delta.places
    assert copy.inside == "town" and copy.x is None
    after = applied(world, delta)
    house = after.places["house"]
    assert house.inside == "town" and abs(house.x) <= 750 and abs(house.y - 8000) <= 450
    assert after.where["Marcus Webb"] == "house"
    delta, refused = resolve(world, reply, says=False)
    assert refused == [f"place The house: {ONLY} puts a place inside another"]
    assert unchanged(world, delta)
    # Never inside itself, or what is inside it.
    loop = {"places": [{"name": "The farm", "inside": "The house", "quote": "Nothing stirred"}]}
    delta, _ = resolve(world, loop, says=True)
    assert delta.moved == [] and applied(world, delta).places["farm"].inside is None


# --- the art, the notice, the fold --------------------------------------------------


def test_a_renamed_building_keeps_its_box_in_its_parent_s_plan():
    world = estate()
    world.places["house"].name = "Hearth House"
    world.places["house"].aliases = ["The house"]
    fixed = fixed_in_plan(world, world.places["farm"])
    house = next(f for f in fixed if f["place_id"] == "house")
    assert house["name"] == "Hearth House" and house["aliases"] == ["The house"]
    spec = {
        "ground": "snow",
        "features": [
            {"kind": "building", "name": "the house", "x": 2, "y": 2, "w": 8, "h": 6,
             "doors": [{"side": "north", "at": 1}]},
            {"kind": "building", "name": "A shed", "x": 30, "y": 2, "w": 4, "h": 3},
        ],
    }  # fmt: skip
    kept = with_fixed(spec, fixed)
    buildings = [f for f in kept["features"] if f["kind"] == "building"]
    assert [f["name"] for f in buildings] == ["A shed", "Hearth House", "The barn"]
    drawn = next(f for f in buildings if f["name"] == "Hearth House")
    assert (drawn["x"], drawn["y"]) == (house["x"], house["y"]) and drawn["place_id"] == "house"
    assert drawn["doors"] == [{"side": "north", "at": 1}]  # the model's door, kept
    # A shape that carries the place's id is that building whatever it is called.
    by_id = {
        "features": [
            {"kind": "building", "name": "whatever", "place_id": "house", "x": 2, "y": 2,
             "w": 8, "h": 6, "doors": [{"side": "east", "at": 1}]},
        ]
    }  # fmt: skip
    kept = with_fixed(by_id, fixed)
    assert [f["name"] for f in kept["features"]] == ["Hearth House", "The barn"]
    assert kept["features"][0]["doors"] == [{"side": "east", "at": 1}]


def test_the_notice_says_what_the_direction_did():
    world = estate()
    delta, _ = resolve(
        world,
        {
            "places": [
                {"name": "Hearth House", "was": "the house", "quote": "Hearth House"},
                {"name": "The farm", "size": 800, "quote": "800 metres"},
                {"name": "The loft", "inside": "Hellsville", "quote": "Nothing stirred"},
                {"name": "The barn", "mark": "⌖(300, -200)", "quote": "The barn now"},
            ],
            "gone": [{"name": "Hellsville", "quote": "Nothing stirred"}],
        },
        says=True,
        marked={"barn": (300.0, -200.0)},
    )
    assert describe_delta(world, delta) == (
        "gone: Hellsville; The house now Hearth House; The loft moved; The barn moved; "
        "The farm now 800 m; The loft now in Hellsville"
    )


def test_the_fold_is_the_same_after_save_and_load_and_on_a_line_without_the_place():
    world = estate()
    delta, _ = resolve(
        world,
        {
            "places": [
                {"name": "Hearth House", "was": "the house", "quote": "Hearth House"},
                {"name": "The barn", "mark": "⌖(300, -200)", "quote": "The barn now"},
            ],
            "gone": [{"name": "The loft", "quote": "Nothing stirred"}],
        },
        says=True,
        marked={"barn": (300.0, -200.0)},
    )
    stored = WorldDelta.model_validate_json(delta.model_dump_json())
    once = applied(world, delta)
    assert applied(world, stored).model_dump() == once.model_dump()
    assert applied(world.model_copy(deep=True), stored, in_place=True).model_dump() == (
        once.model_dump()
    )
    # Another take's line never had the barn or the loft: the delta names
    # them, and the fold simply has them absent, nothing brought back.
    other = estate()
    for pid in ("barn", "loft"):
        other.places.pop(pid)
    other.where.pop("Jane")
    other.links = other.links[:1]
    other.things = {}
    folded = applied(other, stored)
    assert "barn" not in folded.places and "loft" not in folded.places
    assert folded.places["house"].name == "Hearth House"


# --- through a session --------------------------------------------------------------


def direction(text: str) -> TurnRequest:
    return TurnRequest(
        speaker_id=DIRECTOR_SPEAKER_ID, user_text=text, controlled_character_id="char-serrik"
    )


def map_section(prompt: str) -> str:
    block = prompt[prompt.index("# THE MAP HERE") :]
    return block.split("\n# ")[0]


def test_a_direction_takes_a_place_off_the_map_and_the_storyteller_is_told(tmp_path, story, cast):
    gone = json.dumps({"gone": [{"name": "The house", "quote": "The house burns to the ground"}]})
    session = make_session(
        tmp_path, story, cast, ["The house burns to nothing.", gone, "Serrik looks about.", "{}"]
    )
    session.config.scene_reads = "manual"
    events = list(session.send(direction("The house burns to the ground; nothing is left of it.")))
    read = session.provider.requests[-1].messages[-1].text
    assert "THE AUTHOR'S TURN: DIRECTION (WHAT IT SAYS HAPPENS)" in read
    assert "THE AUTHOR'S TURN IS DIRECTION OR NARRATION" in read and '"gone"' in read
    world = session.world()
    assert "house" not in world.places and world.where["Serrik Vaun"] == "farm"
    assert "Map: gone: The house" in [e.text for e in events if isinstance(e, SessionNotice)]
    assert session.path()[-1].meta.area.spots["Serrik Vaun"].area == "farm"

    list(session.send(turn("I look about.")))
    sent = map_section(session.provider.requests[-2].messages[-1].text)
    assert "Serrik Vaun is at: The farm" in sent and "The house" not in sent
    assert "DIRECTION OR NARRATION" not in session.provider.requests[-1].messages[-1].text
    kinds = [e["kind"] for e in read_api_log(session.story.id, root=tmp_path)]
    assert "world_refused" not in kinds


def test_a_direction_moves_a_known_place_to_a_marked_spot_and_its_plan_follows_its_size(
    tmp_path, story, cast
):
    session = make_session(tmp_path, story, cast)
    session.config.scene_reads = "manual"
    token = session.mark_of_tile("farm", 100, 50)
    point = marks_in(token)[0]
    read = json.dumps(
        {
            "places": [
                {"name": "The house", "mark": token, "size": 60, "quote": "The house now stands at"}
            ]
        }
    )
    session.provider.responses = ["The house stood there now.", read, "Serrik looks.", "{}"]
    list(session.send(direction(f"The house now stands at {token}, and it is 60 metres across.")))
    world = session.world()
    house = world.places["house"]
    assert (house.x, house.y) == (point.x, point.y) and house.inside == "farm"
    assert house.size == 60 and session.world_art.redraw == ["house"]  # its plan drawn again
    assert world.where["Serrik Vaun"] == "house"  # he was inside: it moved with him
    assert session.path()[-1].meta.area.spots["Serrik Vaun"].area == "house"
    box = next(f for f in fixed_in_plan(world, world.places["farm"]) if f["place_id"] == "house")
    assert box["x"] > 25  # the farm's plan has its box where it stands now
    # The storyteller is told the geography as it is now: the house's old
    # spot is open farm.
    old = session.mark_of_tile("farm", 66, 27)  # about where the house stood
    assert marks_in(old)[0].x < 40
    list(session.send(turn(f"I look toward {old}.")))
    sent = map_section(session.provider.requests[-2].messages[-1].text)
    assert "Serrik Vaun is at: The house, in The farm" in sent
    line = next(ln for ln in sent.splitlines() if ln.startswith(f"- {old}: "))
    assert "in The farm" in line and "in The house" not in line
