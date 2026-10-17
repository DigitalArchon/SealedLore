"""People placed but not yet named: Unknown 1, 2, 3 on the map until a passage
names them (the author, Oct 17 2026). Live, a girl came down the farmhouse
stairs, was never named, and so was never on the map: nobody to talk to."""

from __future__ import annotations

import json

from sealedlore.engine.world import applied, resolve_reply
from sealedlore.models.character import Character
from sealedlore.models.world import WorldDelta
from tests.test_area import make_session, turn, world_and_art

SOURCE = (
    "A teenage girl comes down the stairs, a knife in her hand. "
    "Behind her, a man in a red coat. The girl says her name is Elena."
)
PEOPLE = [Character(id="e", name="Elena Ruiz"), Character(id="s", name="Serrik Vaun")]


def where(who: str, quote: str = "A teenage girl comes down the stairs") -> dict:
    return {"who": who, "place": "The house", "quote": quote}


def test_a_description_is_placed_as_unknown_1_and_known_again_by_it_or_its_label():
    world, _art = world_and_art()
    delta, refused = resolve_reply(
        world,
        {"where": [where("the teenage girl"), where("A man in a red coat", "a man in a red coat")]},
        source=SOURCE,
        people=PEOPLE,
    )
    assert refused == []
    assert delta.where == {"Unknown 1": "house", "Unknown 2": "house"}
    assert delta.unknowns == {"Unknown 1": "the teenage girl", "Unknown 2": "a man in a red coat"}
    world = _fold(world, delta)
    # The next read: by description (any article) or by label, the same people.
    again, _ = resolve_reply(
        world,
        {"where": [where("a teenage girl"), where("unknown2", "a man in a red coat")]},
        source=SOURCE,
        people=PEOPLE,
    )
    assert again.where == {"Unknown 1": "house", "Unknown 2": "house"} and again.unknowns == {}


def test_no_one_person_and_a_label_never_given_are_refused():
    world, _art = world_and_art()
    delta, refused = resolve_reply(
        world,
        {"where": [where("everyone"), where("Unknown 7"), where("the teenage girl", "no such")]},
        source=SOURCE,
        people=PEOPLE,
    )
    assert delta.where == {} and delta.unknowns == {}
    assert len(refused) == 3


def test_a_passage_naming_an_unknown_makes_them_that_person_and_the_label_is_never_reused():
    world, _art = world_and_art()
    first, _ = resolve_reply(
        world, {"where": [where("the teenage girl")]}, source=SOURCE, people=PEOPLE
    )
    world = _fold(world, first)
    named, refused = resolve_reply(
        world,
        {"named": [{"who": "Unknown 1", "name": "Elena", "quote": "her name is Elena"}]},
        source=SOURCE,
        people=PEOPLE,
    )
    assert refused == [] and named.named == {"Unknown 1": "Elena Ruiz"}
    world = _fold(world, named)
    assert world.where["Elena Ruiz"] == "house" and "Unknown 1" not in world.where
    assert world.unknowns == {}
    later, _ = resolve_reply(
        world,
        {"where": [where("a man in a red coat", "a man in a red coat")]},
        source=SOURCE,
        people=PEOPLE,
    )
    assert later.where == {"Unknown 2": "house"}  # never Unknown 1 again


def test_unknown_people_stand_on_the_map_and_reach_the_storyteller_by_their_description(
    tmp_path, story, cast
):
    read = json.dumps({"where": [where("the teenage girl")]})
    session = make_session(tmp_path, story, cast, [SOURCE, read, "She waits.", "{}"])
    session.config.scene_reads = "manual"
    list(session.send(turn("I call up the stairs.")))
    assert session.world().unknowns == {"Unknown 1": "the teenage girl"}
    assert "Unknown 1" in session.area_state().spots  # drawn on the play view
    sent = session.map_context(session.path(), "char-serrik")
    assert "the teenage girl (not named yet)" in sent and "Unknown 1" not in sent
    assert session.map_action("talk", "Unknown 1") == session.texts.fill(
        "map.turn.talk", name="the teenage girl"
    )
    # The next read is told who isn't named yet, and how to name them.
    list(session.send(turn("I wait.")))
    asked = session.provider.requests[-1].messages[-1].text
    assert "Unknown 1 (the teenage girl)" in asked and '"named"' in asked


def _fold(world, delta: WorldDelta):
    return applied(world, delta)


def test_a_read_may_stand_an_unknown_at_a_spot_by_their_description(tmp_path, story, cast):
    """Live, the read placed the girl and stood her "at the doorway" by her
    description: the spot was dropped, since no one in the story was called so."""
    read = json.dumps(
        {"where": [where("the teenage girl")], "at": {"the teenage girl": "the cast-iron stove"}}
    )
    session = make_session(tmp_path, story, cast, [SOURCE, read])
    session.config.scene_reads = "manual"
    list(session.send(turn("I call up the stairs.")))
    girl = session.path()[-1].meta.area.spots["Unknown 1"]
    stove = next(p for p in session.area("house").placements if "stove" in p.name)
    assert abs(girl.x - stove.x) + abs(girl.y - stove.y) <= 2


def test_someone_named_with_no_card_is_still_stood_at_a_spot(tmp_path, story, cast):
    """Live (Oct 17 2026): "Unknown 1" was named Ruth, who has no card, and
    from then on the read's spots for her were dropped: she stayed in the
    kitchen while the passages had her bring John a mug by the stove."""
    first = json.dumps({"where": [where("the woman", "A woman")]})
    named = json.dumps(
        {"named": [{"who": "Unknown 1", "name": "Ruth", "quote": "her name is Ruth"}]}
    )
    moved = json.dumps(
        {
            "where": [{"who": "Ruth", "place": "The house", "quote": "Ruth brings the mug over"}],
            "at": {"Ruth": "the cast-iron stove"},
        }
    )
    session = make_session(
        tmp_path,
        story,
        cast,
        [
            "A woman stands at the far wall.",
            first,
            "She says her name is Ruth.",
            named,
            "Ruth brings the mug over to the stove.",
            moved,
        ],
    )
    session.config.scene_reads = "manual"
    for text in ("I wave.", "I ask her name.", "I sit by the stove."):
        list(session.send(turn(text)))
    assert "Ruth" in session.world().where
    ruth = session.path()[-1].meta.area.spots["Ruth"]
    stove = next(p for p in session.area("house").placements if "stove" in p.name)
    assert abs(ruth.x - stove.x) + abs(ruth.y - stove.y) <= 2
