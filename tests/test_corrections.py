"""Quick corrections on the map (engine/session_corrections.py; the author,
Oct 17 2026: "quick manual editing features to make it easier for the user
to correct AI mistakes"): people, things and places put right by hand, with
no model call, kept on the latest message and undone in one step."""

from __future__ import annotations

from sealedlore.models.area import LiveArea, Spot
from sealedlore.models.world import WorldDelta, WorldPlace, WorldThing
from tests.test_area import make_session, turn

KETTLE = {"kind": "object", "name": "a copper kettle", "x": 28, "y": 5}


def started(tmp_path, story, cast, **kw):
    session = make_session(tmp_path, story, cast, ["Quiet.", "{}"] * 4, **kw)
    session.config.scene_reads = "manual"
    list(session.send(turn("I look around.")))
    return session


def put(session, name: str, x: int, y: int, area: str = "house") -> None:
    state = session.area_state()
    state.spots[name] = Spot(area=area, x=x, y=y)
    session.story.area_live = LiveArea(leaf_id=session.story.active_leaf_id, state=state)


def test_someone_moved_by_hand_stands_there_and_undo_puts_them_back(tmp_path, story, cast):
    session = started(tmp_path, story, cast)
    put(session, "Maela Orr", 26, 7)
    assert session.correct_move_person("Maela Orr", "house", 30, 8)
    assert session.area_state().spots["Maela Orr"] == Spot(area="house", x=30, y=8)
    assert session.correction_undo == "Maela Orr moved"
    # A wall, or someone else's tile, is refused.
    me = session.area_state().spots["Serrik Vaun"]
    assert not session.correct_move_person("Maela Orr", "house", me.x, me.y)
    assert not session.correct_move_person("Maela Orr", "house", 0, 0)
    assert session.undo_correction()
    assert session.area_state().spots["Maela Orr"] == Spot(area="house", x=26, y=7)
    assert session.correction_undo is None


def test_moved_out_of_the_house_the_map_and_the_roster_follow(tmp_path, story, cast):
    session = started(tmp_path, story, cast)
    put(session, "Maela Orr", 26, 7)
    assert "char-maela" in session.story.scene.present_character_ids
    farm = session.area("farm")
    x, y = next(
        (x, y) for y in range(farm.height) for x in range(farm.width) if farm.walkable(x, y)
    )
    assert session.correct_move_person("Maela Orr", "farm", x, y)
    assert session.world().where["Maela Orr"] == "farm"
    assert "char-maela" not in session.story.scene.present_character_ids
    # Kept on the latest message: the next turn carries it, and the next
    # read starts from it.
    list(session.send(turn("I wait.")))
    assert session.world().where["Maela Orr"] == "farm"


def test_someone_not_here_is_off_the_map_and_the_card(tmp_path, story, cast):
    session = started(tmp_path, story, cast)
    put(session, "Maela Orr", 26, 7)
    assert session.correct_not_here("Maela Orr")
    assert "Maela Orr" not in session.area_state().spots
    assert "Maela Orr" not in session.world().where
    assert "char-maela" not in session.story.scene.present_character_ids
    assert not session.correct_not_here("Serrik Vaun")  # never the author's character
    assert session.undo_correction()
    assert "char-maela" in session.story.scene.present_character_ids
    assert "Maela Orr" in session.area_state().spots


def test_an_unknown_named_and_a_wrong_name_put_right(tmp_path, story, cast):
    session = started(tmp_path, story, cast)
    leaf = session.path()[-1]
    leaf.meta.world = WorldDelta(
        unknowns={"Unknown 1": "the watchwoman"}, where={"Unknown 1": "house"}
    )
    put(session, "Unknown 1", 26, 7)
    assert session.correct_name("Unknown 1", "Ruth")
    world = session.world()
    assert world.unknowns == {} and world.where["Ruth"] == "house"
    assert "Ruth" in session.area_state().spots
    # Ruth was wrong too: she is someone not named yet after all.
    assert session.correct_name("Ruth", "", description="the watchwoman with the rifle")
    world = session.world()
    assert "Ruth" not in world.where and world.unknowns == {
        "Unknown 2": "the watchwoman with the rifle"
    }
    assert "Unknown 2" in session.area_state().spots
    # And then she is Maela.
    assert session.correct_name("Unknown 2", "Maela Orr")
    assert session.world().where["Maela Orr"] == "house"
    assert not session.correct_name("Maela Orr", "Serrik Vaun")  # not the author's own


def test_a_thing_moved_renamed_broken_and_removed_by_hand(tmp_path, story, cast):
    session = started(tmp_path, story, cast, house_objects=[KETTLE])
    house = session.area("house")
    stove = next(p for p in house.placements if "stove" in p.name)
    # The author's word moves even what is fixed in place.
    assert session.correct_move_thing(stove.id, "house", 30, 10)
    moved = next(p for p in session.area("house").placements if p.id == stove.id)
    assert (moved.x, moved.y) == (30, 10)
    kettle = next(p for p in house.placements if "kettle" in p.name)
    assert session.correct_rename_thing(kettle.id, "house", "a dented tin kettle")
    renamed = next(p for p in session.area("house").placements if p.id == kettle.id)
    assert renamed.name == "a dented tin kettle" and (renamed.x, renamed.y) == (kettle.x, kettle.y)
    assert session.correct_broken(kettle.id, "house", True)
    assert next(p for p in session.area("house").placements if p.id == kettle.id).broken
    assert session.correct_broken(kettle.id, "house", False)
    assert not next(p for p in session.area("house").placements if p.id == kettle.id).broken
    assert session.correct_remove_thing(kettle.id, "house")
    assert not any(p.id == kettle.id for p in session.area("house").placements)
    assert session.undo_correction()
    assert any(p.id == kettle.id for p in session.area("house").placements)


def test_a_thing_put_down_by_hand_is_on_the_map_and_the_play_view(tmp_path, story, cast):
    session = started(tmp_path, story, cast)
    thing_id = session.correct_put_thing("a coil of rope", "house", 30, 10)
    assert thing_id is not None
    rope = next(p for p in session.area("house").placements if p.id == thing_id)
    assert (rope.x, rope.y) == (30, 10) and "take" in rope.verbs
    assert session.world().things[thing_id].at == "house"
    assert session.correct_remove_thing(thing_id, "house")
    assert thing_id not in session.world().things


def test_a_map_thing_removed_by_hand_leaves_the_map_s_list(tmp_path, story, cast):
    anvil = WorldThing(id="anvil", name="a heavy iron anvil", at="house")
    session = started(tmp_path, story, cast, things=[anvil])
    assert session.correct_remove_thing("anvil", "house")
    assert "anvil" not in session.world().things
    assert not any(p.id == "anvil" for p in session.area("house").placements)


def test_places_renamed_removed_merged_put_inside_and_moved(tmp_path, story, cast):
    session = started(tmp_path, story, cast)
    leaf = session.path()[-1]
    kitchen = WorldPlace(id="k", name="The farmhouse kitchen", kind="room", inside="house")
    shed = WorldPlace(id="s", name="The shed", kind="building", inside="farm", x=60.0, y=10.0)
    leaf.meta.world = WorldDelta(
        places=[kitchen, shed],
        added=["k", "s"],
        where={"Maela Orr": "k"},
        things=[WorldThing(id="pot", name="a stew pot", at="k")],
    )
    assert session.correct_rename_place("s", "The tool shed")
    world = session.world()
    assert world.places["s"].name == "The tool shed" and "The shed" in world.places["s"].aliases
    # The read's kitchen is the house itself: merged, Maela and the pot go there.
    assert session.correct_merge_place("k", "house")
    world = session.world()
    assert "k" not in world.places and world.where["Maela Orr"] == "house"
    assert world.things["pot"].at == "house"
    assert "The farmhouse kitchen" in world.places["house"].aliases
    assert not session.correct_merge_place("farm", "house")  # never into what is inside it
    assert session.correct_place_inside("s", None)
    assert session.world().places["s"].inside is None
    assert session.correct_move_place("s", 80.0, 20.0)
    assert (session.world().places["s"].x, session.world().places["s"].y) == (80.0, 20.0)
    assert session.correct_remove_place("s")
    assert "s" not in session.world().places


def test_nothing_is_corrected_in_a_private_scene_or_before_the_story_begins(
    tmp_path, story, cast, monkeypatch
):
    session = make_session(tmp_path, story, cast)
    assert session.correction_problem() == "The story hasn't begun."
    session = started(tmp_path / "b", story, cast)
    assert session.correction_problem() is None
    monkeypatch.setattr(type(session), "in_private", property(lambda _self: True))
    assert "private" in session.correction_problem()


def test_undo_is_for_the_message_it_was_made_at(tmp_path, story, cast):
    session = started(tmp_path, story, cast)
    put(session, "Maela Orr", 26, 7)
    assert session.correct_move_person("Maela Orr", "house", 30, 8)
    list(session.send(turn("I wait.")))
    assert session.correction_undo is None and not session.undo_correction()
