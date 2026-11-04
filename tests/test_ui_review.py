"""What a look over the window before 3.0.0b2 found, held: nothing typed is
lost without a question, Enter in a field never saves a whole dialog, the
keyboard reaches what it should, and what the plot hides stays hidden."""

# ruff: noqa: F811 - the window's fixtures come from test_ui_tidy

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui.prompt_editor import PromptEditorDialog  # noqa: E402
from sealedlore.gui.setup_dialog import SetupDialog  # noqa: E402
from sealedlore.gui.transcript import MessageWidget  # noqa: E402
from tests.test_ui_tidy import app, window  # noqa: E402, F401


def asked(monkeypatch, answer) -> list[str]:
    """Answers every question with `answer`, keeping their titles."""
    titles: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, title, *_a, **_k: titles.append(title) or answer,
    )
    return titles


def open_edit(app, window):
    widget = window.transcript.message_widget("u1")
    assert isinstance(widget, MessageWidget)
    widget.begin_edit()
    app.processEvents()
    widget._editor.setFocus()
    app.processEvents()
    return widget


def test_ctrl_enter_in_a_message_being_edited_saves_it_and_sends_nothing(app, window):
    window.composer.input.setPlainText("A turn waiting in the box.")
    widget = open_edit(app, window)
    widget._editor.setPlainText("The turn, changed.")
    before = len(window.session.nodes)
    QApplication.focusWidget().setFocus()
    window.send_turn()  # what Ctrl+Enter runs
    app.processEvents()
    node = next(n for n in window.session.nodes if n.id == "u1")
    assert node.content == "The turn, changed." and len(window.session.nodes) == before
    assert window.composer.text() == "A turn waiting in the box.", "the draft stays unsent"


def test_esc_in_a_changed_edit_asks_and_an_unchanged_one_just_closes(app, window, monkeypatch):
    titles = asked(monkeypatch, QMessageBox.Cancel)
    widget = open_edit(app, window)
    window._on_escape()
    assert not widget.editing() and titles == [], "nothing changed: closed, nothing asked"
    widget = open_edit(app, window)
    widget._editor.setPlainText("Typed and not saved.")
    window._on_escape()
    assert widget.editing() and titles == ["Discard this edit?"], "Cancel keeps it"


def test_leaving_the_story_with_an_edit_open_asks_first(app, window, monkeypatch):
    titles = asked(monkeypatch, QMessageBox.Cancel)
    widget = open_edit(app, window)
    widget._editor.setPlainText("Typed and not saved.")
    story_id = window.session.story.id
    window.close_story()
    assert window.session is not None and window.session.story.id == story_id
    assert titles == ["Discard your edit?"] and widget.editing()


def test_removing_a_card_asks_and_defaults_to_no(app, window, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _p, title, _t, buttons=None, default=None: (
            seen.append((title, default)) or QMessageBox.No
        ),
    )
    panel = window.cast_panel
    before = len(window.session.cast)
    panel._select(window.session.cast[0].id)
    panel._remove_character()
    assert len(window.session.cast) == before
    assert seen and seen[0] == ("Remove this character?", QMessageBox.No)


def test_enter_in_the_prompt_editors_search_saves_nothing(app, window, monkeypatch):
    dialog = PromptEditorDialog(window.config, window.session.story, parent=window)
    dialog.show()
    dialog.search.setFocus()
    dialog.search.setText("remember")
    for kind in (QKeyEvent.KeyPress, QKeyEvent.KeyRelease):
        QApplication.sendEvent(dialog.search, QKeyEvent(kind, Qt.Key_Return, Qt.NoModifier))
    app.processEvents()
    assert dialog.isVisible() and dialog.result() == 0, "still open, nothing saved"
    # Cancel with nothing changed closes at once; with a change, it asks.
    titles = asked(monkeypatch, QMessageBox.Cancel)
    key = next(iter(dialog.edits["app"]), None) or "rules.length_line"
    dialog.edits["app"][key] = dialog.edits["app"].get(key) or _edit_of(key)
    dialog.reject()
    assert dialog.isVisible() and titles == ["Discard your changes?"]
    dialog.edits["app"].pop(key)
    dialog.reject()
    assert not dialog.isVisible()


def _edit_of(key: str):
    from sealedlore.models.prompt_edit import PromptEdit

    return PromptEdit(text="Something else entirely.")


def test_setup_asks_before_cancel_loses_an_edit_and_enter_never_saves(app, window, monkeypatch):
    dialog = SetupDialog(window.session.story, window.session.cast, parent=window)
    dialog.show()
    for _ in range(3):
        app.processEvents()
    dialog.title.setFocus()
    for kind in (QKeyEvent.KeyPress, QKeyEvent.KeyRelease):
        QApplication.sendEvent(dialog.title, QKeyEvent(kind, Qt.Key_Return, Qt.NoModifier))
    app.processEvents()
    assert dialog.isVisible(), "Enter in the title doesn't save and close Setup"
    titles = asked(monkeypatch, QMessageBox.Cancel)
    dialog.title.textEdited.emit("A new title")  # as typing it does
    dialog.reject()
    assert dialog.isVisible() and titles == ["Discard your changes?"]
    dialog.close()


def test_a_plot_hidden_character_is_offered_nowhere_before_they_come(app, window):
    session = window.session
    hidden = session.cast[-1]
    dialog = SetupDialog(session.story, session.cast, parent=window, hidden=frozenset({hidden.id}))
    assert hidden not in dialog.cast and session.cast[0] in dialog.cast
    dialog.close()


# --- the review's last items (after 3.0.0b2) -------------------------------------------


def test_the_changes_dialog_marks_the_words_that_differ():
    from sealedlore.gui.card_changes import marked

    pieces = marked(
        "She trusts nobody, and keeps the farm.", "She trusts John slowly, and keeps the farm."
    )
    assert pieces == [
        ("She trusts ", False),
        ("nobody,", True),
        (" and keeps the farm.", False),
    ]
    assert marked("Same words.", "Same words.") == [("Same words.", False)]
