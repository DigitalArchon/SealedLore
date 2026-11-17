"""Main and supporting characters told apart in the window (gui/character_roles.py,
gui/cast_panel.py, gui/people_page.py): the author, Nov 15 2026, found players
couldn't tell the two kinds apart."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from sealedlore.engine.rules import voicing
from sealedlore.gui.character_roles import (
    MAIN,
    MAIN_HEADING,
    MAKE_MAIN,
    MAKE_SUPPORTING,
    SUPPORTING,
    SUPPORTING_HEADING,
    apply_played_by,
    played_by,
)
from sealedlore.gui.people_page import people_view
from sealedlore.models.character import Character
from sealedlore.models.scene import SceneState


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize(
    ("playable", "author_only", "choice", "role"),
    [
        (True, False, "either", "either"),
        (True, True, "author", "author"),
        (False, False, "storyteller", "storyteller"),
        # The pair no card should hold reads as voicing reads it.
        (False, True, "author", "author"),
    ],
)
def test_played_by_is_the_two_flags_as_voicing_reads_them(playable, author_only, choice, role):
    jane = Character(name="Jane Moss", is_player_available=playable, author_only=author_only)
    assert played_by(jane) == choice
    assert voicing(jane, jane.id, None) == role
    copy = Character(name="Jane Moss")
    apply_played_by(copy, choice)
    assert voicing(copy, copy.id, None) == role
    assert played_by(copy) == choice


def test_each_card_says_which_kind_and_offers_the_other(app):
    from sealedlore.gui.cast_panel import CastPanel

    jane = Character(name="Jane Moss")
    marcus = Character(name="Marcus Webb", is_player_available=False)
    panel = CastPanel()
    panel.set_cast([jane], [marcus])
    headings = [
        panel.list.item(row).text()
        for row in range(panel.list.count())
        if panel.list.item(row).data(Qt.UserRole) is None
    ]
    assert headings == [MAIN_HEADING, SUPPORTING_HEADING]

    panel.select_character(jane.id)
    assert panel.role_title.text() == MAIN
    assert panel.move_button.text() == MAKE_SUPPORTING
    assert not panel.role_box.isHidden() and not panel.played_by_box.isHidden()

    panel.select_character(marcus.id)
    assert panel.role_title.text() == SUPPORTING
    assert panel.move_button.text() == MAKE_MAIN
    # A supporting character's flags reach no prompt: nothing to choose.
    assert panel.played_by_box.isHidden()

    moved: list[str] = []
    panel.promote_requested.connect(moved.append)
    panel.move_button.click()
    assert moved == [marcus.id]


def test_played_by_sets_a_main_characters_flags(app):
    from sealedlore.gui.cast_panel import CastPanel

    jane = Character(name="Jane Moss")
    panel = CastPanel()
    panel.set_cast([jane])
    panel.select_character(jane.id)
    combo = panel.played_by.combo
    combo.setCurrentIndex(combo.findData("storyteller"))
    assert (jane.is_player_available, jane.author_only) == (False, False)
    combo.setCurrentIndex(combo.findData("author"))
    assert (jane.is_player_available, jane.author_only) == (True, True)
    assert "never writes" in panel.played_by.about.text()


def test_a_supporting_card_keeps_its_flags_when_edited(app):
    """Hidden, the choice never writes a supporting card's flags over."""
    from sealedlore.gui.cast_panel import CastPanel

    marcus = Character(name="Marcus Webb", is_player_available=False, author_only=True)
    panel = CastPanel()
    panel.set_cast([], [marcus])
    panel.select_character(marcus.id)
    panel.summary.setPlainText("The leader of Hellsville.")
    assert marcus.summary == "The leader of Hellsville."
    assert (marcus.is_player_available, marcus.author_only) == (False, True)


def test_your_characters_card_has_the_same_choice(app):
    from sealedlore.gui.card_form import CharacterForm

    jane = Character(name="Jane Moss", is_player_available=False)
    form = CharacterForm(lambda: None)
    form.load(jane)
    assert form.played_by.value() == "storyteller"
    assert form.played_by.about.text().startswith("In a story they join, you can't play them")
    combo = form.played_by.combo
    combo.setCurrentIndex(combo.findData("either"))
    assert (jane.is_player_available, jane.author_only) == (True, False)


def test_the_people_page_tags_each_kind():
    john = Character(id="john", name="John Carver")
    jane = Character(id="jane", name="Jane Moss")
    marcus = Character(id="marcus", name="Marcus Webb")
    elena = Character(id="elena", name="Elena Ruiz")
    scene = SceneState(present_character_ids=["john"], present_others=["Marcus Webb"])
    view = people_view([john, jane], [marcus, elena], scene, "john")
    assert [(p.name, p.role) for p in view.here] == [
        ("John Carver", "main"),
        ("Marcus Webb", "supporting"),
    ]
    assert [(p.name, p.role) for p in view.away] == [("Jane Moss", "main")]
    assert [(p.name, p.role) for p in view.others] == [("Elena Ruiz", "supporting")]
