"""Things fixed in place stay put unless the author moves them (the author,
Oct 17 2026): only Direction, Narration or a turn under Fiat may. Heavy
things that aren't fixed (a table, an anvil, a boulder) are movable: the
story decides whether an attempt succeeds, and the map follows it
(engine/takeable.py `thing_class`)."""

from __future__ import annotations

import json
from dataclasses import replace

from sealedlore.engine.takeable import (
    can_be_taken,
    fixed_thing,
    is_vehicle,
    item_verbs,
    movable_thing,
    thing_class,
)
from sealedlore.engine.world import resolve_reply
from sealedlore.models.world import WorldThing
from tests.test_area import make_session, turn, world_and_art

SOURCE = (
    "Serrik carries the rifle and the town's stores into the house, rolls the boulder in "
    "after them, then drives the truck. The old well stays where it is."
)
STORES = WorldThing(id="stores", name="The town's stores", at="town")
RIFLE = WorldThing(id="rifle", name="A rifle", at="town")
TRUCK = WorldThing(id="truck", name="Pickup truck", at="town")
BOULDER = WorldThing(id="boulder", name="a large boulder", at="town")
WELL = WorldThing(id="well", name="The old well", at="town")


def world_with_things():
    world, _art = world_and_art()
    world.things = {t.id: t.model_copy() for t in (STORES, RIFLE, TRUCK, BOULDER, WELL)}
    return world


def moved(name: str, at: str = "The house", quote: str = SOURCE[:40]) -> dict:
    return {"name": name, "at": at, "quote": quote}


def test_what_can_be_taken_is_what_the_play_view_offers_take_for():
    assert can_be_taken("the amulet") and can_be_taken("a punched brass card")
    assert can_be_taken("rusted coffee tin") and can_be_taken("A rifle")
    assert not can_be_taken("The town's stores") and not can_be_taken("a heavy oak table")
    assert not can_be_taken("a tinted window") and not can_be_taken("a carpet")
    assert "take" in item_verbs("the amulet")
    # Driven or ridden, not taken: the read may move them with whoever is aboard.
    assert is_vehicle("Pickup truck") and not fixed_thing("Pickup truck")
    assert not is_vehicle("a carpet") and not is_vehicle("a card")


def test_heavy_things_are_movable_and_only_what_is_built_in_or_rooted_is_fixed():
    for name in (
        "a heavy iron anvil",
        "a large boulder",
        "a heavy oak table",
        "a wooden chest",
        "rough wooden bench",
        "The town's stores",
        "a pine dresser",
        "a barrel",
    ):
        assert thing_class(name) == "movable" and movable_thing(name), name
    for name in (
        "a cast-iron stove",
        "a stone hearth",
        "The old well",
        "the bar counter",
        "an apple tree",
        "the old oak",
        "a bolted iron bench",
        "a statue",
        "a door",
    ):
        assert thing_class(name) == "fixed" and fixed_thing(name), name
    assert thing_class("A rifle") == "carried" and thing_class("Pickup truck") == "vehicle"


def test_an_ordinary_turn_moves_what_isn_t_fixed_and_never_what_is():
    world = world_with_things()
    names = ("The town's stores", "A rifle", "Pickup truck", "a large boulder", "The old well")
    reply = {"things": [moved(n) for n in names]}
    delta, refused = resolve_reply(world, reply, source=SOURCE, people=[])
    by_id = {t.id: t for t in delta.things}
    assert "well" not in by_id  # its move refused and nothing else changed
    for moved_id in ("stores", "rifle", "truck", "boulder"):
        assert by_id[moved_id].at == "house", moved_id
    assert len(refused) == 1 and "The old well" in refused[0] and "Fiat" in refused[0]
    # Dragged off by someone is refused too (where they are, the house).
    world.where["Serrik Vaun"] = "house"
    carried = {"things": [moved("The old well", "carried by Serrik Vaun")]}
    delta, refused = resolve_reply(world, carried, source=SOURCE, people=[])
    assert delta.things == [] and refused
    # Its state may change where it stands.
    spoilt = {"things": [{**moved("The old well"), "state": "poisoned"}]}
    delta, refused = resolve_reply(world, spoilt, source=SOURCE, people=[])
    assert [(t.at, t.state) for t in delta.things] == [("town", "poisoned")] and refused


def test_a_heavy_thing_carried_by_someone_is_dragged_to_where_they_are():
    world = world_with_things()
    world.where["Serrik Vaun"] = "house"
    reply = {"things": [moved("a large boulder", "carried by Serrik Vaun")]}
    delta, refused = resolve_reply(world, reply, source=SOURCE, people=[])
    (boulder,) = delta.things
    assert (boulder.at, boulder.carried_by) == ("house", None) and refused == []
    reply = {"things": [moved("A rifle", "carried by Serrik Vaun")]}
    delta, _refused = resolve_reply(world, reply, source=SOURCE, people=[])
    assert [(t.at, t.carried_by) for t in delta.things] == [(None, "Serrik Vaun")]


def test_the_author_s_word_moves_it():
    world = world_with_things()
    reply = {"things": [moved("The old well")]}
    for kwargs in ({"author_says": True}, {"moves_fixed": True}):
        delta, refused = resolve_reply(world, reply, source=SOURCE, people=[], **kwargs)
        assert [t.at for t in delta.things] == ["house"] and refused == []


def test_a_turn_under_fiat_moves_it_and_an_ordinary_one_doesn_t(tmp_path, story, cast):
    well = WELL.model_copy(update={"at": "farm"})
    read = json.dumps({"things": [moved("The old well", quote="drags the old well")]})
    passage = "He drags the old well into the house."
    session = make_session(tmp_path, story, cast, [passage, read] * 2, things=[well])
    session.config.scene_reads = "manual"
    list(session.send(turn("I drag the well inside.")))
    found = [t for t in session.world().things.values() if t.name == well.name]
    assert [t.at for t in found] == ["farm"]
    list(session.send(replace(turn("I drag the well inside."), agency_mode="fiat")))
    found = [t for t in session.world().things.values() if t.name == well.name]
    assert [t.at for t in found] == ["house"]


def _beside_stove(session):
    stove = next(p for p in session.area("house").placements if "stove" in p.name)
    session.walked(session.walk_route(int(stove.centre[0]), int(stove.centre[1]), beside=True))


def _tail(session) -> str:
    """The storyteller's last request's tail (the reads come after it)."""
    told = [r for r in session.provider.requests if "REMEMBER:" in r.messages[-1].text]
    return told[-1].messages[-1].text


def test_the_storyteller_is_told_what_stays_put_on_a_turn_that_can_t_move_it(tmp_path, story, cast):
    """Live (Oct 17 2026): the prose dragged an anvil across the yard on an
    ordinary turn and the map kept it where it was. On such a turn what can't
    be taken is marked in reach, said once, and named on REMEMBER when the
    turn names it."""
    session = make_session(tmp_path, story, cast, ["It doesn't budge.", "{}"] * 4)
    session.config.scene_reads = "manual"
    _beside_stove(session)
    list(session.send(turn("I put my shoulder to the stove and shove it at the door.")))
    tail = _tail(session)
    assert "cast-iron stove (" in tail and "fixed in place)" in tail
    assert tail.count(session.texts["map.fixed"]) == 1
    remember = tail[tail.index("REMEMBER:") :]
    assert "the cast-iron stove" in remember
    # A turn that names nothing fixed: marked, but nothing on REMEMBER.
    list(session.send(turn("I warm my hands.")))
    tail = _tail(session)
    assert "fixed in place" in tail and "unmoved" not in tail[tail.index("REMEMBER:") :]


def test_fiat_and_direction_are_told_nothing_stays_put(tmp_path, story, cast):
    from sealedlore.models.node import DIRECTOR_SPEAKER_ID

    session = make_session(tmp_path, story, cast, ["It moves.", "{}"] * 3)
    session.config.scene_reads = "manual"
    _beside_stove(session)
    list(session.send(replace(turn("I shove the stove at the door."), agency_mode="fiat")))
    tail = _tail(session)
    assert "fixed in place" not in tail and "unmoved" not in tail
    direction = replace(turn("The stove now stands by the door."), speaker_id=DIRECTOR_SPEAKER_ID)
    list(session.send(direction))
    tail = _tail(session)
    assert "fixed in place" not in tail and "unmoved" not in tail


def test_remember_names_the_thing_the_turn_names_best(tmp_path, story, cast):
    """Live: "the heavy wooden shelf unit" brought every shelf in the house
    onto REMEMBER with it (a shelf unit is movable now: the hearth stands
    in for what is fixed)."""
    shelves = [
        {"kind": "object", "name": "a heavy stone hearth", "x": 10, "y": 3},
        {"kind": "object", "name": "a bread oven", "x": 12, "y": 3},
        {"kind": "object", "name": "a heavy wooden shelf unit", "x": 14, "y": 3},
    ]
    session = make_session(tmp_path, story, cast, ["Quiet.", "{}"], house_objects=shelves)
    session.config.scene_reads = "manual"
    list(session.send(turn("I look around.")))  # nobody stands anywhere before a passage
    said = session.fixed_reminder(
        session.path(), "char-serrik", text="I shove the heavy stone hearth at the window."
    )
    assert "stone hearth" in said and "oven" not in said
    # The shelf unit is heavy but movable: nothing stops it.
    said = session.fixed_reminder(
        session.path(), "char-serrik", text="I shove the heavy shelf unit at the window."
    )
    assert said == "" or "shelf" not in said
    assert session.fixed_reminder(session.path(), "char-serrik", text="I sit down.") == ""
