"""Looks (engine/looks.py, engine/session_looks.py): which of their pictures
each character is shown with, kept on the messages as the scene is, and the
author's choice of one."""

from __future__ import annotations

from pathlib import Path

import pytest

from sealedlore.engine.looks import labels_in_use, look_words, looks_at, shown_picture
from sealedlore.models.character import Character
from sealedlore.models.image import ImageRef
from sealedlore.models.node import Node
from tests.test_continue import make_session, turn
from tests.test_private import build as build_private
from tests.test_private import enter as enter_private
from tests.test_private import play
from tests.test_private import turn as private_turn

PORTRAIT = ImageRef(id="p", file="images/refs/p.png", caption="portrait")
ARMOUR = ImageRef(id="a", file="images/refs/a.png", look="combat armour")
ANGRY = ImageRef(id="g", file="images/refs/g.png", look="combat armour", expression="angry")


def with_pictures(character: Character) -> Character:
    character.reference_images = [PORTRAIT.model_copy(), ARMOUR.model_copy(), ANGRY.model_copy()]
    return character


def test_the_nearest_choice_above_counts():
    first = Node(kind="user", speaker_id="char-serrik", content="a")
    second = Node(kind="assistant", speaker_id="__narrator__", content="b")
    third = Node(kind="user", speaker_id="char-serrik", content="c")
    second.meta.looks = {"char-serrik": "a"}
    assert looks_at([first]) == {}
    assert looks_at([first, second, third]) == {"char-serrik": "a"}
    third.meta.looks = {}
    assert looks_at([first, second, third]) == {}


def test_a_character_shows_their_chosen_picture_while_it_is_theirs():
    serrik = with_pictures(Character(id="char-serrik", name="Serrik Vaun"))
    assert shown_picture(serrik, {}).id == "p"
    assert shown_picture(serrik, {"char-serrik": "g"}).id == "g"
    # Taken off their card since: back to the portrait.
    assert shown_picture(serrik, {"char-serrik": "gone"}).id == "p"
    assert shown_picture(Character(name="Nobody"), {}) is None


def test_a_picture_is_named_by_its_labels():
    assert look_words(ANGRY) == "combat armour · angry"
    assert look_words(ARMOUR) == "combat armour"
    assert look_words(PORTRAIT) == "portrait"
    assert look_words(ImageRef(file="x.png"), 3) == "Picture 3"
    assert labels_in_use([PORTRAIT, ARMOUR, ANGRY]) == (["combat armour"], ["angry"])


def test_a_choice_goes_on_the_latest_message_and_follows_the_story(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, ["The door gives.", "It holds."])
    with_pictures(session.cast[0])
    with pytest.raises(ValueError, match="once the story has begun"):
        session.show_look("char-serrik", "a")
    list(session.send(turn()))
    session.show_look("char-serrik", "g")
    assert session.looks_now() == {"char-serrik": "g"}
    assert session.full_path()[-1].meta.looks == {"char-serrik": "g"}
    # Another take of the passage was written without it: the choice stays
    # with the take it was made on.
    list(session.regenerate())
    assert session.looks_now() == {}
    session.show_look("char-serrik", None)
    assert session.looks_now() == {}
    with pytest.raises(ValueError, match="isn't one of Serrik Vaun's"):
        session.show_look("char-serrik", "not-a-picture")


def test_in_a_private_scene_a_choice_stays_on_the_scene_s_messages(tmp_path: Path):
    session, _main, held = build_private(tmp_path)
    play(session, private_turn(held, "I walk into town."))
    with_pictures(next(c for c in session.cast if c.id == held))
    public = session.full_path()[-1]
    enter_private(session)
    # On the story's message before the scene it would outlast the scene.
    with pytest.raises(ValueError, match="once the scene has a message of its own"):
        session.show_look(held, "g")
    play(session, private_turn(held, "I lower my voice."))
    session.show_look(held, "g")
    assert public.meta.looks is None
    assert session.full_path()[-1].meta.private_span is not None
    assert session.looks_now() == {held: "g"}
    # Ended, its choices count no more, and a choice made after it carries
    # none of them onto the story's messages.
    for _event in session.close_private("They talked quietly."):
        pass
    assert session.looks_now() == {}
    session.show_look(held, "a")
    assert session.full_path()[-1].meta.looks == {held: "a"}


# --- drawing new looks ------------------------------------------------------------------


def test_a_look_s_change_is_asked_in_the_labels_words():
    from sealedlore.engine.looks import look_change

    assert look_change(look="combat armour", expression="angry") == (
        "Change their clothes or state to: combat armour. "
        "Change their expression or pose to: angry."
    )
    assert look_change(expression="  in a  fighting stance ") == (
        "Change their expression or pose to: in a fighting stance."
    )
    assert look_change() == ""


def test_a_picture_drawn_for_a_look_lands_on_the_card_labelled(tmp_path: Path, story, cast):
    from sealedlore.models.image import GeneratedImage

    session = make_session(tmp_path, story, cast, [])
    serrik = with_pictures(session.cast[0])
    session.pictures.write("images/refs/p.png", b"portrait")
    request = session.card_request(
        serrik,
        "Edit this picture: …",
        model="m",
        info=None,
        first=False,
        source=serrik.reference_images[0],
        look="combat armour",
        expression="laughing",
    )
    assert (request.card_look, request.card_expression) == ("combat armour", "laughing")
    session.pictures.write("images/drawn.png", b"drawn")
    session.pictures.add_images(
        [
            GeneratedImage(
                file="images/drawn.png",
                prompt=request.prompt,
                model="m",
                size="1024x1024",
                card_id=serrik.id,
                card_look=request.card_look,
                card_expression=request.card_expression,
            )
        ]
    )
    session.take_card_pictures()
    landed = serrik.reference_images[-1]
    assert (landed.look, landed.expression) == ("combat armour", "laughing")


def test_a_scene_picture_offers_each_character_as_they_look_now(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, ["The door gives."])
    with_pictures(session.cast[0])
    list(session.send(turn()))
    session.show_look("char-serrik", "g")
    serrik = [c.use for c in session.image_ref_choices() if c.use.owner_id == "char-serrik"]
    assert serrik[0].ref_id == "g"
    assert serrik[0].caption == "as they look now: combat armour · angry"
    assert [use.caption for use in serrik[1:]] == ["portrait", "combat armour"]


# --- chosen after a passage ---------------------------------------------------------------

import json  # noqa: E402

from sealedlore.engine.look_pick import (  # noqa: E402
    build_pick_messages,
    look_subjects,
    parse_pick,
)


def pick_reply(**chosen: str) -> str:
    return json.dumps(
        {
            "looks": [
                {"name": name, "picture": handle, "missing": ""} for name, handle in chosen.items()
            ]
        }
    )


def test_a_pick_concerns_those_here_or_named_with_more_than_one_picture():
    serrik = with_pictures(Character(id="char-serrik", name="Serrik Vaun"))
    maela = with_pictures(Character(id="char-maela", name="Maela Orr"))
    idris = Character(id="char-idris", name="Captain Idris")  # one picture at most
    subjects = look_subjects(
        [serrik, maela, idris], {"char-serrik": "a"}, {"char-serrik"}, "Maela laughs."
    )
    assert [s.name for s in subjects] == ["Serrik Vaun", "Maela Orr"]
    assert subjects[0].now == "L2" and subjects[1].now == "L4"
    assert [o.handle for o in subjects[1].options] == ["L4", "L5", "L6"]
    away = look_subjects([maela], {}, {"char-serrik"}, "Nobody comes.")
    assert away == []
    messages = build_pick_messages(subjects, author_turn="I wave.", passage="Maela laughs.")
    text = messages[1].parts[0].text
    assert "Serrik Vaun (shown now as L2)\n- L1: their portrait\n- L2: combat armour" in text


def test_a_pick_is_held_to_each_character_s_own_pictures():
    serrik = with_pictures(Character(id="char-serrik", name="Serrik Vaun"))
    maela = with_pictures(Character(id="char-maela", name="Maela Orr"))
    subjects = look_subjects([serrik, maela], {}, {"char-serrik", "char-maela"}, "")
    reply = json.dumps(
        {
            "looks": [
                {"name": "serrik vaun", "picture": "l3", "missing": "combat armour · laughing"},
                {"name": "Maela Orr", "picture": "L2", "missing": "none"},  # Serrik's
                {"name": "Somebody Else", "picture": "L1"},
            ]
        }
    )
    picks = parse_pick(reply, subjects)
    assert [(p.character_id, p.ref_id, p.missing) for p in picks] == [
        ("char-serrik", "g", "combat armour · laughing")
    ]
    with pytest.raises(ValueError):
        parse_pick("no idea", subjects)


def test_after_a_passage_each_one_here_is_shown_as_the_pick_says(tmp_path: Path, story, cast):
    session = make_session(
        tmp_path,
        story,
        cast,
        ["Serrik's jaw tightens; he draws his sword.", pick_reply(**{"Serrik Vaun": "L3"})],
    )
    with_pictures(session.cast[0])
    notices = [e.text for e in session.send(turn()) if hasattr(e, "text")]
    passage = session.full_path()[-1]
    assert passage.meta.looks == {"char-serrik": "g"}
    assert "Looks: Serrik Vaun: combat armour · angry" in notices
    sent = session.provider.requests[-1]
    assert sent.model == session.scene_model
    assert "THE LATEST PASSAGE:\n\nSerrik's jaw tightens" in sent.messages[1].parts[-1].text
    log = (tmp_path / "stories" / story.id / "api_log.jsonl").read_text()
    assert '"look_request"' in log and '"look_response"' in log


def test_no_pick_is_made_for_a_story_without_looks(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, ["The door gives."])
    list(session.send(turn()))
    assert len(session.provider.requests) == 1  # the passage alone
    session.config.look_picks = "manual"
    with_pictures(session.cast[0])
    session.provider.responses.append("It holds.")
    list(session.regenerate())
    assert len(session.provider.requests) == 2


def test_a_failed_pick_leaves_them_as_they_were(tmp_path: Path, story, cast):
    session = make_session(tmp_path, story, cast, ["The door gives.", "no json here"])
    with_pictures(session.cast[0])
    notices = [e.text for e in session.send(turn()) if hasattr(e, "text")]
    assert any("Couldn't choose the looks" in n for n in notices)
    assert session.looks_now() == {}


def test_in_a_private_scene_the_looks_are_chosen_on_a_private_model_only(tmp_path: Path):
    session, main, held = build_private(tmp_path)
    play(session, private_turn(held, "I walk into town."))
    character = next(c for c in session.cast if c.id == held)
    with_pictures(character)
    private = enter_private(session)
    sent_to_main = len(main.requests)
    private.responses = ["She laughs, low.", pick_reply(**{character.name: "L2"})]
    play(session, private_turn(held, "I tell her the joke."))
    assert len(main.requests) == sent_to_main
    assert private.requests[-1].model == "local/model"
    assert session.full_path()[-1].meta.looks == {held: "a"}
    # Settings' private model for looks, only when the scene could move to it:
    # a TEE scene's looks never go to a model that isn't.
    span = session.open_span.model_copy(update={"model": "TEE/kimi-k3"})
    session.config.private_look_model = "zai/glm-5.3"
    assert session._private_look_model(span) == "TEE/kimi-k3"
    session.config.private_look_model = "TEE/glm-5.3-flash"
    assert session._private_look_model(span) == "TEE/glm-5.3-flash"


# --- the faces on each passage -------------------------------------------------------------

from sealedlore.engine.looks import faces_along  # noqa: E402
from sealedlore.models.scene import SceneState  # noqa: E402


def test_each_passage_carries_who_was_there_as_they_looked():
    serrik = with_pictures(Character(id="char-serrik", name="Serrik Vaun"))
    maela = with_pictures(Character(id="char-maela", name="Maela Orr"))
    turn1 = Node(kind="user", speaker_id="char-serrik", content="I wait.")
    turn1.meta.controlled_character_id = "char-serrik"
    first = Node(kind="assistant", speaker_id="__narrator__", content="Nothing.")
    turn2 = Node(kind="user", speaker_id="char-serrik", content="I call Maela.")
    second = Node(kind="assistant", speaker_id="__narrator__", content="Maela comes in.")
    second.meta.scene = SceneState(present_character_ids=["char-serrik", "char-maela"])
    second.meta.looks = {"char-serrik": "g"}
    private = Node(kind="assistant", speaker_id="__narrator__", content="Quietly.")
    private.meta.private_span = "span-1"
    private.meta.looks = {"char-serrik": "a"}
    after = Node(kind="assistant", speaker_id="__narrator__", content="Later.")
    path = [turn1, first, turn2, second, private, after]
    faces = faces_along(path, [serrik, maela], held_id="char-serrik")
    assert [(c.id, r.id) for c, r in faces[first.id]] == [("char-serrik", "p")]
    assert [(c.id, r.id) for c, r in faces[second.id]] == [
        ("char-serrik", "g"),
        ("char-maela", "p"),
    ]
    # An ended private scene's looks don't carry on; an open one's do.
    assert [r.id for _c, r in faces[after.id]][0] == "g"
    open_faces = faces_along(path, [serrik, maela], held_id="char-serrik", open_span="span-1")
    assert [r.id for _c, r in open_faces[after.id]][0] == "a"
