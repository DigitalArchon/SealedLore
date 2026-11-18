"""The own-model trial in the window: the card's choice, the Playing list,
the answers under a passage (hidden while the round is open, unless shown as
a spoiler), and editing one to redo the turn with it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QLabel  # noqa: E402

from sealedlore.gui.card_form import CharacterForm  # noqa: E402
from sealedlore.gui.character_roles import OWN, PlayedBy, played_by  # noqa: E402
from sealedlore.gui.note_dialog import NoteDialog  # noqa: E402
from sealedlore.gui.transcript import ANSWERS_HIDDEN  # noqa: E402
from sealedlore.gui.window_answers import EDIT_KEEP, EDIT_REDO  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from tests.test_background_queue import _send, _window, wait_for  # noqa: E402

MODEL = "Local::maela-lora"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def own_window(tmp_path: Path, story, cast, responses: list[str]):
    cast[1].own_model = MODEL  # Maela, in the scene
    cast[1].is_player_available = False
    return _window(tmp_path, story, cast, responses)


def answer_texts(window, node_id: str) -> list[str]:
    box = window.transcript.message_widget(node_id).answers_box
    return [label.text() for label in box.findChildren(QLabel)]


def answer_dialog(monkeypatch, text: str | None, seen: list) -> None:
    def run(dialog: NoteDialog) -> int:
        seen.append(dialog)
        if text is None:
            return QDialog.Rejected
        dialog.text.setPlainText(text)
        return QDialog.Accepted

    monkeypatch.setattr(NoteDialog, "exec", run)


# --- the card --------------------------------------------------------------


def test_their_own_model_is_a_way_to_play_a_main_character(app) -> None:
    control = PlayedBy()
    character = Character(id="m", name="Maela")
    control.show_character(character)
    assert not control.model.isVisibleTo(control)
    control.combo.setCurrentIndex(control.combo.findData(OWN))
    assert control.model.isVisibleTo(control)
    assert "Name the model" in control.about.text()
    control.model.setText(MODEL)
    control.apply_to(character)
    assert character.own_model == MODEL
    assert not character.is_player_available and not character.author_only
    assert played_by(character) == OWN
    # Any other choice takes the model away.
    control.combo.setCurrentIndex(control.combo.findData("either"))
    control.apply_to(character)
    assert character.own_model is None and character.is_player_available


def test_your_characters_cards_don_t_offer_it(app) -> None:
    form = CharacterForm(lambda: None)
    assert form.played_by.combo.findData(OWN) == -1


def test_the_cast_board_s_card_keeps_the_model(app, tmp_path: Path, story, cast) -> None:
    window = _window(tmp_path, story, cast, ["The door gives."])
    panel = window.cast_panel
    panel.select_character("char-maela")
    panel.played_by.combo.setCurrentIndex(panel.played_by.combo.findData(OWN))
    panel.played_by.model.setText(MODEL)
    maela = next(c for c in window.session.cast if c.id == "char-maela")
    assert maela.own_model == MODEL and not maela.is_player_available
    window._refresh_composer()
    # Off the Playing list: the author doesn't play them while their model does.
    playing = [window.composer.held.itemData(i) for i in range(window.composer.held.count())]
    assert "char-maela" not in playing
    window.close()


# --- the answers in the story --------------------------------------------------


def test_the_open_round_s_answers_are_hidden_until_asked(app, tmp_path: Path, story, cast):
    window = own_window(tmp_path, story, cast, ["The door gives.", "{}", "MAELA-PICKS-THE-LOCK"])
    assert window.answers_action.isVisible()
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    assert passage.meta.reactions[0].text == "MAELA-PICKS-THE-LOCK"
    shown = " ".join(answer_texts(window, passage.id))
    assert ANSWERS_HIDDEN.format(names="Maela Orr") in shown
    assert "MAELA-PICKS-THE-LOCK" not in shown
    # View → Show the characters' answers: the spoiler, on request.
    window.answers_action.setChecked(True)
    assert "MAELA-PICKS-THE-LOCK" in answer_texts(window, passage.id)
    window.close()


def test_once_answered_they_are_shown_with_the_passage(app, tmp_path: Path, story, cast):
    window = own_window(
        tmp_path,
        story,
        cast,
        ["The door gives.", "{}", "MAELA-PICKS-THE-LOCK", "The lock turns.", "{}", "Hm."],
    )
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    first = window.session.path()[-1]
    _send(window, "I hold the lamp for her.")
    wait_for(app, lambda: not window._busy)
    assert "MAELA-PICKS-THE-LOCK" in answer_texts(window, first.id)
    window.close()


def test_the_view_switch_is_offered_only_with_an_own_model(app, tmp_path: Path, story, cast):
    window = _window(tmp_path, story, cast, ["The door gives."])
    assert not window.answers_action.isVisible()
    window.close()


# --- editing an answer ---------------------------------------------------------


def test_editing_the_open_round_s_answer_keeps_it_for_the_next_turn(
    app, tmp_path: Path, story, cast, monkeypatch
):
    window = own_window(
        tmp_path, story, cast, ["The door gives.", "{}", "I pick the lock.", "It holds.", "{}"]
    )
    window.answers_action.setChecked(True)
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    seen: list = []
    answer_dialog(monkeypatch, "I step back, hands up.", seen)
    sent = len(window._provider.requests)

    window.transcript.message_widget(passage.id).answer_buttons[0].click()

    assert seen[0].go.text() == EDIT_KEEP
    assert seen[0].text.toPlainText() == "I step back, hands up."
    assert passage.meta.reactions[0].text == "I step back, hands up."
    assert len(window._provider.requests) == sent  # nothing written yet
    # The next turn carries the edited answer to the storyteller.
    _send(window, "I hold the lamp.")
    wait_for(app, lambda: not window._busy)
    storyteller = window._provider.requests[sent]
    assert "I step back, hands up." in storyteller.messages[-1].text
    window.close()


def test_editing_an_older_answer_writes_the_passage_after_it_again(
    app, tmp_path: Path, story, cast, monkeypatch
):
    window = own_window(
        tmp_path,
        story,
        cast,
        [
            "The door gives.",
            "{}",
            "I pick the lock.",
            "The lock turns.",
            "{}",
            "Hm.",
            "She steps back; the lock stays shut.",
            "{}",
            "Hm.",
        ],
    )
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    answered = window.session.path()[-1]
    _send(window, "I hold the lamp.")
    wait_for(app, lambda: not window._busy)
    after = window.session.path()[-1]
    seen: list = []
    answer_dialog(monkeypatch, "I step back, hands up.", seen)

    window.transcript.message_widget(answered.id).answer_buttons[0].click()
    wait_for(app, lambda: not window._busy)

    assert seen[0].go.text() == EDIT_REDO
    take = window.session.path()[-1]
    assert take.id != after.id and take.content == "She steps back; the lock stays shut."
    assert "I step back, hands up." in window.session.last_prompt.section("tail.round").text
    window.close()


def test_cancelling_an_edit_changes_nothing(app, tmp_path: Path, story, cast, monkeypatch):
    window = own_window(tmp_path, story, cast, ["The door gives.", "{}", "I pick the lock."])
    window.answers_action.setChecked(True)
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    passage = window.session.path()[-1]
    answer_dialog(monkeypatch, None, [])
    window.transcript.message_widget(passage.id).answer_buttons[0].click()
    assert passage.meta.reactions[0].text == "I pick the lock."
    assert passage.meta.reactions[0].edited_at is None
    window.close()


# --- where it is chosen ------------------------------------------------------


def test_played_by_stands_at_the_top_of_a_main_character_s_card(
    app, tmp_path: Path, story, cast
) -> None:
    """In the box that says they are a main character, above the fields: as
    the form's last row, under the skills, a real card had it below the fold."""
    window = _window(tmp_path, story, cast, [])
    panel = window.cast_panel
    panel.select_character("char-maela")
    assert panel.played_by.parent() is panel.played_by_box
    assert panel.played_by_box.parent() is panel.role_box
    assert panel._form.indexOf(panel.played_by) == -1
    window.close()


def _start_window(tmp_path: Path, story, cast):
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.storage.repository import StoryBundle, save_story_bundle

    story.held_character_id = None
    story.active_leaf_id = None
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.scene_reads = "manual"
    window.config.suggest_characters = False
    window.open_story(story.id)
    return window


def test_start_offers_a_model_of_their_own_for_every_main_character(
    app, tmp_path: Path, story, cast
) -> None:
    from sealedlore.gui.start_dialog import StartDialog

    dialog = StartDialog(story, cast)
    players = dialog.players
    assert set(players.rows) == {c.id for c in cast}
    dialog.play_as.setCurrentIndex(dialog.play_as.findData("char-serrik"))
    # The character played can't be one.
    serrik_tick, _ = players.rows["char-serrik"]
    assert not serrik_tick.isEnabled() and not serrik_tick.isChecked()
    tick, model = players.rows["char-maela"]
    assert not model.isEnabled()
    tick.setChecked(True)
    # Ticked with no model: Start waits for one, and says so.
    assert not dialog.start_button.isEnabled()
    assert "Maela Orr" in players.problem.text()
    model.setText(MODEL)
    assert dialog.start_button.isEnabled()
    assert dialog.own_models() == {"char-maela": (MODEL, None)}
    # Choosing to play someone with a model of their own takes it away.
    dialog.play_as.setCurrentIndex(dialog.play_as.findData("char-maela"))
    assert dialog.own_models() == {}
    dialog.deleteLater()


def test_a_model_chosen_at_start_plays_them_from_the_opening(
    app, tmp_path: Path, story, cast, monkeypatch
) -> None:
    from sealedlore.gui import main_window as window_module

    window = _start_window(tmp_path, story, cast)

    def run(dialog) -> int:
        dialog.play_as.setCurrentIndex(dialog.play_as.findData("char-serrik"))
        tick, model = dialog.players.rows["char-maela"]
        tick.setChecked(True)
        model.setText(MODEL)
        return QDialog.Accepted

    monkeypatch.setattr(window_module.StartDialog, "exec", run)
    window.start_story()
    maela = next(c for c in window.session.cast if c.id == "char-maela")
    assert played_by(maela) == OWN and maela.own_model == MODEL
    playing = [window.composer.held.itemData(i) for i in range(window.composer.held.count())]
    assert "char-maela" not in playing
    window.close()


def test_unticking_at_start_hands_them_back(app, tmp_path: Path, story, cast) -> None:
    from sealedlore.gui.start_dialog import StartDialog
    from sealedlore.gui.start_players import apply_chosen

    cast[1].own_model = MODEL
    cast[1].is_player_available = False
    dialog = StartDialog(story, cast)
    tick, model = dialog.players.rows["char-maela"]
    assert tick.isChecked() and model.text() == MODEL
    tick.setChecked(False)
    assert dialog.own_models() == {"char-maela": None}
    apply_chosen(cast, dialog.own_models())
    assert played_by(cast[1]) == "either" and cast[1].own_model is None
    dialog.deleteLater()


NANO = "https://nano-gpt.com/api/v1"
ROUTE_MODEL = "z-ai/glm-5.3"


def _routed():
    from sealedlore.models.route import ModelRoute

    helpers = dict(
        route_endpoint=lambda model: NANO,
        hosts_of=lambda model: ("one-host", "another-host"),
    )
    return ModelRoute(priority="speed"), helpers


def test_the_card_s_own_model_takes_a_route_and_keeps_it(app) -> None:
    """Every model field with a route offers one; their own model's too."""
    route, helpers = _routed()
    played = PlayedBy()
    played.set_browse(lambda current: None, **helpers)
    character = Character(name="Maela Orr")
    played.combo.setCurrentIndex(played.combo.findData(OWN))
    played.model.setText(ROUTE_MODEL)
    assert played.model.route_button.isVisibleTo(played)
    saved = []
    played.changed.connect(lambda: saved.append(True))
    played.model.set_route(route)
    assert saved  # a route chosen is an edit, saved as one
    played.apply_to(character)
    assert character.own_model == ROUTE_MODEL and character.own_route == route
    # Shown again, and taken away with the model.
    other = PlayedBy()
    other.set_browse(lambda current: None, **helpers)
    other.show_character(character)
    assert other.model.route == route
    other.combo.setCurrentIndex(other.combo.findData("either"))
    other.apply_to(character)
    assert character.own_model is None and character.own_route is None


def test_start_s_rows_take_a_route_too(app, story, cast) -> None:
    from sealedlore.gui.start_dialog import StartDialog
    from sealedlore.gui.start_players import apply_chosen

    route, helpers = _routed()
    routes = (lambda current, r: None, helpers["route_endpoint"], helpers["hosts_of"])
    dialog = StartDialog(story, cast, routes=routes)
    tick, model = dialog.players.rows["char-maela"]
    tick.setChecked(True)
    model.setText(ROUTE_MODEL)
    model.set_route(route)
    assert dialog.own_models() == {"char-maela": (ROUTE_MODEL, route)}
    apply_chosen(cast, dialog.own_models())
    assert cast[1].own_route == route
    dialog.deleteLater()


# --- they answer after the author's turn -------------------------------------


def test_after_the_turn_their_answers_stand_under_it_in_the_open(
    app, tmp_path: Path, story, cast
) -> None:
    story.defaults.round_order = "after"
    window = own_window(tmp_path, story, cast, ["MAELA-HOLDS-THE-LAMP", "The door gives.", "{}"])
    # Nothing to keep hidden: they answer what the author has sent.
    assert not window.answers_action.isVisible()
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    turn_node, passage = window.session.path()[-2:]
    assert turn_node.meta.round_after and not passage.meta.reactions
    assert "MAELA-HOLDS-THE-LAMP" in " ".join(answer_texts(window, turn_node.id))
    window.close()


def test_setup_and_start_choose_when_they_answer(
    app, tmp_path: Path, story, cast, monkeypatch
) -> None:
    from sealedlore.gui import main_window as window_module
    from sealedlore.gui.setup_dialog import SetupDialog

    dialog = SetupDialog(story, cast)
    assert dialog.round_order.currentData() == "together"
    dialog.round_order.setCurrentIndex(dialog.round_order.findData("after"))
    dialog._save()
    assert story.defaults.round_order == "after"
    dialog.deleteLater()

    story.defaults.round_order = "together"
    window = _start_window(tmp_path, story, cast)

    def run(start) -> int:
        start.players.round_order.setCurrentIndex(start.players.round_order.findData("after"))
        return QDialog.Accepted

    monkeypatch.setattr(window_module.StartDialog, "exec", run)
    window.start_story()
    assert window.session.story.defaults.round_order == "after"
    window.close()


# --- what their model is sent, and the author's say in it --------------------


def test_their_model_s_prompt_can_be_seen_and_changed(app, tmp_path: Path, story, cast) -> None:
    from sealedlore.engine.reactions import fingerprint, seen_entries
    from sealedlore.gui.character_prompt_dialog import CharacterPromptDialog
    from sealedlore.models.memory import Recollection

    window = own_window(tmp_path, story, cast, ["The door gives.", "{}", "I pick the lock."])
    _send(window, "I put my shoulder to the door.")
    wait_for(app, lambda: not window._busy)
    session = window.session
    maela = next(c for c in session.cast if c.id == "char-maela")
    # A memory note over what she has seen so far.
    seen = seen_entries(
        session.path(), maela, session.visible_cast(), start=story.setup.starting_scene
    )
    session.bundle.memories.append(
        Recollection(
            character_id=maela.id,
            covered_node_ids=[node_id for node_id, _ in seen],
            fingerprint=fingerprint(seen),
            content="I saw the door give.",
        )
    )
    # The card's button opens it, for a character with a model named.
    window.cast_panel.select_character(maela.id)
    assert window.cast_panel.played_by.prompt_button.isVisibleTo(window.cast_panel.played_by)

    dialog = CharacterPromptDialog(session, maela.id)
    assert "You are Maela Orr" in dialog.sent.toPlainText()
    assert "I saw the door give." in dialog.sent.toPlainText()
    dialog.notes.setPlainText("Never trusts the watch.")
    [(memory_id, box)] = dialog.memories
    box.setPlainText("I saw the door give, and Serrik push.")
    assert dialog.save()
    assert maela.own_notes == "Never trusts the watch."
    memory = next(m for m in session.bundle.memories if m.id == memory_id)
    assert memory.content == "I saw the door give, and Serrik push." and memory.edited_at
    # The view follows, and her next request carries both.
    shown = dialog.sent.toPlainText()
    assert "HOW TO PLAY Maela Orr, FROM THE AUTHOR" in shown and "Serrik push" in shown
    dialog.deleteLater()
    window.close()


def test_closing_with_changes_asks_first(app, tmp_path: Path, story, cast, monkeypatch) -> None:
    from PySide6.QtWidgets import QMessageBox

    from sealedlore.gui.character_prompt_dialog import CharacterPromptDialog

    window = own_window(tmp_path, story, cast, [])
    dialog = CharacterPromptDialog(window.session, "char-maela")
    dialog.notes.setPlainText("Something typed.")
    asked, closed = [], []
    dialog.finished.connect(closed.append)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: asked.append(a) or QMessageBox.Cancel
    )
    dialog.reject()
    assert asked and not closed  # kept open: Cancel
    dialog.deleteLater()
    window.close()
