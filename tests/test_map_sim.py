"""Moving on the map is a turn (branch map-sim, Oct 23 2026): a click writes
the turn, nothing moves until it is sent, and the play view walks the figure
after. The engine's side; the window's is in tests/test_area_view.py."""

from __future__ import annotations

import json

from sealedlore.engine.area import Area
from sealedlore.engine.world import applied, resolve_reply
from tests.test_area import make_session, turn, world_and_art
from tests.test_outside import KNOCK, at_house


def _stand(session, area_id: str, x: int, y: int) -> None:
    from sealedlore.models.area import LiveArea, Spot

    state = session.area_state()
    state.spots["Serrik Vaun"] = Spot(area=area_id, x=x, y=y)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)


def test_going_over_to_someone_names_them_and_walks_up_when_the_turn_goes(tmp_path, story, cast):
    """Live (Brasshaven, Oct 23 2026): Go over to them wrote "I walk over to
    ⌖(-2, 0)." for the captain."""
    session = make_session(tmp_path, story, cast, ["She nods.", "{}"])
    session.config.scene_reads = "manual"
    me = session.area_state().spots["Serrik Vaun"]
    other = session.area_state().spots["Maela Orr"]
    assert other.area == me.area
    text = session.go_to_tile(other.x, other.y, beside=True, name="Maela Orr")
    assert text == "I walk over to Maela Orr."
    action = session.story.area_live.state.action
    assert action.verb == "approach" and action.target == "Maela Orr"
    assert action.spot is not None and (action.spot.x, action.spot.y) != (other.x, other.y)
    assert abs(action.spot.x - other.x) <= 1 and abs(action.spot.y - other.y) <= 1
    assert session.area_state().spots["Serrik Vaun"] == me  # not until it is sent
    list(session.send(turn(text)))
    stamped = session.path()[-2].meta.area
    assert stamped.spots["Serrik Vaun"] == action.spot
    sent = session.provider.requests[0].messages[-1].text
    assert "has walked up and stands beside Maela Orr now" in sent
    assert stamped.walked_words.endswith("on foot")  # a few steps: less than a minute


def test_a_building_the_plan_alone_draws_is_entered_by_its_door(tmp_path, story, cast):
    """Brasshaven's works and depots are drawn on the Row's plan with no
    place of their own: clicking one writes "I go into X." at its door, and
    the story takes the character in."""
    session = make_session(tmp_path, story, cast, ["She goes in.", "{}"])
    session.config.scene_reads = "manual"
    rows = [
        "_______________",
        "__#######______",
        "__#.....#______",
        "__#.....+______",
        "__#######______",
        "_______________",
    ]
    yard = Area(
        place_id="farm",
        name="The farm",
        indoor=False,
        look="grass",
        rows=rows,
        tile_m=2.0,
        objects={},
        labels={"The Boilerworks": (4, 2)},
    )
    session.area = lambda *a, **k: yard  # type: ignore[method-assign]
    _stand(session, "farm", 12, 3)
    text = session.go_to_tile(8, 3, name="The Boilerworks")
    assert text == "I go into The Boilerworks."
    action = session.story.area_live.state.action
    assert action.verb == "enter" and (action.spot.x, action.spot.y) == (8, 3)
    assert action.steps[0] == (12, 3) and action.steps[-1] == (8, 3)
    list(session.send(turn(text)))
    stamped = session.path()[-2].meta.area
    assert (stamped.spots["Serrik Vaun"].x, stamped.spots["Serrik Vaun"].y) == (8, 3)
    sent = session.provider.requests[0].messages[-1].text
    assert "gone in through the door of The Boilerworks" in sent


def test_a_read_s_spot_inside_clears_a_stale_outside_flag():
    """Live (Brasshaven, Oct 23 2026): the inspector followed the author's
    character into the Copper Kettle, the read put him at the table twice
    over and left the flag off, and the rule that keeps someone outside
    until the quote takes them in kept him at the door in the Row."""
    world, _art = world_and_art()
    delta, _ = resolve_reply(world, at_house(True), source=KNOCK, people=[])
    world = applied(world, delta)
    assert world.outside == ["Serrik Vaun"]
    quote = "Serrik sat down across from her."
    reply = at_house(None, quote)
    reply["at"] = {"Serrik Vaun": "the round table"}
    delta, _ = resolve_reply(world, reply, source=quote, people=[])
    assert delta.outside == {"Serrik Vaun": False}
    # A spot at the door is still outside.
    reply["at"] = {"Serrik Vaun": "the doorway"}
    delta, _ = resolve_reply(world, reply, source=quote, people=[])
    assert delta.outside == {"Serrik Vaun": True}


def test_leaving_an_area_from_anywhere_walks_to_its_edge_first(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["He walks on.", "{}"])
    session.config.scene_reads = "manual"
    farm = session.area("farm")
    _stand(session, "farm", 20, 12)
    assert session.edge_here() is None
    text = session.go_past_edge("north")
    assert text == "I walk on north, into open country."
    action = session.story.area_live.state.action
    assert action.verb == "onward" and action.steps[0] == (20, 12) and action.steps[-1][1] == 0
    assert action.spot.area.startswith("window:") and action.seconds > 0
    assert action.seconds > session.edge_choices("north")[0][2] - 2
    list(session.send(turn(text)))
    stamped = session.path()[-2].meta.area
    assert stamped.spots["Serrik Vaun"].area.startswith("window:")
    assert f"from {farm.name} to open country" in stamped.walked_words
    sent = session.provider.requests[0].messages[-1].text
    assert "walked on north into open country" in sent


def test_a_turn_rewritten_in_the_author_s_own_words_moves_nobody(tmp_path, story, cast):
    session = make_session(tmp_path, story, cast, ["Quiet.", "{}"])
    session.config.scene_reads = "manual"
    me = session.area_state().spots["Serrik Vaun"]
    session.go_to_tile(me.x + 4, me.y)
    list(session.send(turn("I stay by the stove.")))
    stamped = session.path()[-2].meta.area
    assert stamped.action is None and stamped.spots["Serrik Vaun"] == me
    assert json.loads(stamped.model_dump_json())["walked"] == 0
