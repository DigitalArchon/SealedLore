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
