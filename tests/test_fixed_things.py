"""Things that can't be taken stay put unless the author moves them (the
author, Oct 17 2026): only Direction, Narration or a turn under Fiat may.
Live, the map read moved "The town's stores" from Hellsville into the
farmhouse on a passage that only named them."""

from __future__ import annotations

import json
from dataclasses import replace

from sealedlore.engine.takeable import can_be_taken, fixed_thing, is_vehicle, item_verbs
from sealedlore.engine.world import resolve_reply
from sealedlore.models.world import WorldThing
from tests.test_area import make_session, turn, world_and_art

SOURCE = "Serrik carries the rifle and the town's stores into the house, then drives the truck."
STORES = WorldThing(id="stores", name="The town's stores", at="town")
RIFLE = WorldThing(id="rifle", name="A rifle", at="town")
TRUCK = WorldThing(id="truck", name="Pickup truck", at="town")


def world_with_things():
    world, _art = world_and_art()
    world.things = {t.id: t.model_copy() for t in (STORES, RIFLE, TRUCK)}
    return world


def moved(name: str, at: str = "The house", quote: str = SOURCE[:40]) -> dict:
    return {"name": name, "at": at, "quote": quote}


def test_what_can_be_taken_is_what_the_play_view_offers_take_for():
    assert can_be_taken("the amulet") and can_be_taken("a punched brass card")
    assert can_be_taken("rusted coffee tin") and can_be_taken("A rifle")
    assert not can_be_taken("The town's stores") and not can_be_taken("a heavy oak table")
    assert not can_be_taken("a tinted window") and not can_be_taken("a carpet")
    assert ("take" in item_verbs("the amulet")) and fixed_thing("a statue")
    # Driven or ridden, not taken: the read may move them with whoever is aboard.
    assert is_vehicle("Pickup truck") and not fixed_thing("Pickup truck")
    assert not is_vehicle("a carpet") and not is_vehicle("a card")


def test_an_ordinary_turn_moves_what_can_be_taken_and_never_what_can_t():
    world = world_with_things()
    reply = {"things": [moved("The town's stores"), moved("A rifle"), moved("Pickup truck")]}
    delta, refused = resolve_reply(world, reply, source=SOURCE, people=[])
    by_id = {t.id: t for t in delta.things}
    assert "stores" not in by_id  # its move refused and nothing else changed
    assert by_id["rifle"].at == "house" and by_id["truck"].at == "house"
    assert len(refused) == 1 and "The town's stores" in refused[0] and "Fiat" in refused[0]
    # Carried off is moved too.
    carried = {"things": [moved("The town's stores", "carried by Serrik Vaun")]}
    delta, refused = resolve_reply(world, carried, source=SOURCE, people=[])
    assert delta.things == [] and refused
    # Its state may change where it stands.
    spoilt = {"things": [{**moved("The town's stores"), "state": "half spoilt"}]}
    delta, refused = resolve_reply(world, spoilt, source=SOURCE, people=[])
    assert [(t.at, t.state) for t in delta.things] == [("town", "half spoilt")] and refused


def test_the_author_s_word_moves_it():
    world = world_with_things()
    reply = {"things": [moved("The town's stores")]}
    for kwargs in ({"author_says": True}, {"moves_fixed": True}):
        delta, refused = resolve_reply(world, reply, source=SOURCE, people=[], **kwargs)
        assert [t.at for t in delta.things] == ["house"] and refused == []


def test_a_turn_under_fiat_moves_it_and_an_ordinary_one_doesn_t(tmp_path, story, cast):
    stores = STORES.model_copy(update={"at": "farm"})
    read = json.dumps({"things": [moved("The town's stores", quote="drags the town's stores")]})
    passage = "He drags the town's stores into the house."
    session = make_session(tmp_path, story, cast, [passage, read] * 2, things=[stores])
    session.config.scene_reads = "manual"
    list(session.send(turn("I drag the stores inside.")))
    found = [t for t in session.world().things.values() if t.name == stores.name]
    assert [t.at for t in found] == ["farm"]
    list(session.send(replace(turn("I drag the stores inside."), agency_mode="fiat")))
    found = [t for t in session.world().things.values() if t.name == stores.name]
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
