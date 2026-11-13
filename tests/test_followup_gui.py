"""Follow-ups in the window, offscreen (gui/followup_dialog.py,
gui/window_followup.py): a request runs on the worker and its result is
shown in the dialog, which stays open for the next; Undo puts it back; the
menu offers it before a story begins, and for a plot whenever there is one.
The author (Nov 6 2026): "I like the world, but I'd prefer the characters
were more badass"."""

# ruff: noqa: F811 - the window's fixtures come from test_plot_writing_gui

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QMessageBox  # noqa: E402

from sealedlore.gui.followup_dialog import FollowUpDialog  # noqa: E402
from sealedlore.gui.plot_dialog import FOLLOW_UP, PlotDialog, PlotReadyDialog  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402
from tests.test_plot_writing_gui import (  # noqa: E402,F401
    PLOT,
    app,
    model,
    settle,
    wait_until,
    window,
)

pytestmark = pytest.mark.real_follow_up

BADASS = json.dumps(
    {
        "summary": "Everyone is harder now.",
        "changes": [
            {
                "kind": "character",
                "target": "Serrik Vaun",
                "field": "description",
                "value": "A scarred duellist nobody crosses twice.",
            }
        ],
        "cannot_fix": [],
    }
)


def drive(monkeypatch, *steps):
    """FollowUpDialog run as the author would: each step a function of the
    open dialog, then gone past. What each saw, kept."""
    seen: list[FollowUpDialog] = []

    def run(dialog: FollowUpDialog) -> int:
        seen.append(dialog)
        for step in steps:
            step(dialog)
        return QDialog.Accepted

    monkeypatch.setattr(FollowUpDialog, "exec", run)
    return seen


def ask(app, window, text: str):
    def step(dialog: FollowUpDialog) -> None:
        dialog.request.setPlainText(text)
        dialog.send_button.click()
        wait_until(app, lambda: not window._busy and dialog.send_button.isEnabled())
        settle(app)

    return step


def test_a_request_is_done_shown_and_undone_from_the_dialog(app, window, monkeypatch):
    provider = model(window, monkeypatch, BADASS)
    session = window.session
    before = session.cast[0].full_description
    logs: list[str] = []

    def undo(dialog: FollowUpDialog) -> None:
        assert dialog.undo_button.isEnabled()
        logs.append(dialog.log.toPlainText())
        dialog.undo_button.click()
        settle(app)
        logs.append(dialog.log.toPlainText())

    seen = drive(monkeypatch, ask(app, window, "Make them more badass."), undo)
    assert window.follow_up_action.isVisible() and window.follow_up_action.isEnabled()
    window.follow_up_action.trigger()
    assert len(seen) == 1 and len(provider.requests) == 1
    assert "You: Make them more badass." in logs[0]
    assert "Everyone is harder now." in logs[0] and "Serrik Vaun › description" in logs[0]
    assert "The last change is taken back." in logs[1]
    assert session.cast[0].full_description == before
    assert seen[0].request.toPlainText() == ""


def test_the_change_reaches_the_story_and_its_pages(app, window, monkeypatch):
    model(window, monkeypatch, BADASS)
    drive(monkeypatch, ask(app, window, "Harder."))
    window.change_story_with_model()
    assert window.session.cast[0].full_description == "A scarred duellist nobody crosses twice."
    window.right_tabs.setCurrentWidget(window.cast_page)
    window.cast_panel._select(window.session.cast[0].id)
    assert "nobody crosses twice" in window.cast_panel.full_description.toPlainText()


def test_a_failed_request_says_so_and_changes_nothing(app, window, monkeypatch):
    model(window, monkeypatch, "Not JSON at all.")
    logs: list[str] = []
    drive(monkeypatch, ask(app, window, "Harder."), lambda d: logs.append(d.log.toPlainText()))
    window.change_story_with_model()
    assert "Nothing was changed" in logs[0]
    assert window.session.follow_up_to_undo("story") is None


def test_undo_after_the_story_changed_since_asks_first(app, window, monkeypatch):
    model(window, monkeypatch, BADASS)
    asked: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda _p, title, *_a, **_k: asked.append(title) or QMessageBox.No
    )

    def edit_then_undo(dialog: FollowUpDialog) -> None:
        window.session.story.world_bible = "Rewritten by hand."
        dialog.undo_button.click()
        settle(app)

    drive(monkeypatch, ask(app, window, "Harder."), edit_then_undo)
    window.change_story_with_model()
    assert asked == ["Undo the last change?"]
    assert window.session.story.world_bible == "Rewritten by hand.", "No keeps everything"


def test_a_story_under_way_is_left_to_review_settings(app, window, monkeypatch):
    session = window.session
    session.bundle.nodes = make_exchange(1)
    session.story.active_leaf_id = "a0"
    session.save()
    window._update_controls()
    assert not window.follow_up_action.isVisible()
    seen = drive(monkeypatch)
    window.change_story_with_model()
    assert seen == [], "nothing opened"


def test_the_plot_is_changed_from_its_own_dialog_and_kept_hidden(app, window, monkeypatch):
    model(window, monkeypatch, PLOT, PLOT.replace("rides up to", "storms"))
    monkeypatch.setattr(PlotDialog, "exec", lambda dialog: QDialog.Accepted)

    def choose_changes(dialog: PlotReadyDialog) -> int:
        dialog.choice = FOLLOW_UP
        return QDialog.Accepted

    monkeypatch.setattr(PlotReadyDialog, "exec", choose_changes)
    logs: list[str] = []
    seen = drive(
        monkeypatch,
        ask(app, window, "Make the captain's arrival violent."),
        lambda d: logs.append(d.log.toPlainText()),
    )
    window.write_plot_action.trigger()
    wait_until(app, lambda: seen)
    settle(app)
    assert seen[0].target == "plot" and not seen[0].revealed
    assert "The plot is written again with that change." in logs[0]
    assert "storms" not in logs[0], "kept hidden"
    event = window.session.story.plot.events[0]
    assert "storms" in event.tell or "storms" in (event.lead_in_text or "")
    assert window.follow_up_plot_action.isEnabled()


def test_the_plot_s_own_notes_are_said_to_be_there_in_its_dialog_and_never_shown(
    app, window, monkeypatch
):
    secret = "The well under Calder Keep is poisoned, crate ZQX-SECRET and all."
    noted = PLOT.replace("# Facts", f"# Storyteller's notes\n\n{secret}\n\n# Facts")
    again = noted.replace("crate ZQX-SECRET", "cask ZQX-SECRET").replace("rides up to", "storms")
    model(window, monkeypatch, noted, again)
    monkeypatch.setattr(PlotDialog, "exec", lambda dialog: QDialog.Accepted)

    def choose_changes(dialog: PlotReadyDialog) -> int:
        dialog.choice = FOLLOW_UP
        return QDialog.Accepted

    monkeypatch.setattr(PlotReadyDialog, "exec", choose_changes)
    logs: list[str] = []

    def look(dialog: FollowUpDialog) -> None:
        dialog.reveal()
        logs.append(dialog.log.toPlainText() + dialog.view.toPlainText())

    seen = drive(
        monkeypatch,
        lambda d: logs.append(d.log.toPlainText()),
        ask(app, window, "Make the captain's arrival violent."),
        look,
    )
    window.write_plot_action.trigger()
    wait_until(app, lambda: seen)
    settle(app)
    assert "The plot also wrote notes for the storyteller (11 words)" in logs[0]
    story = window.session.story
    assert "cask ZQX-SECRET" in story.storyteller_notes and story.storyteller_notes_by == "model"
    # Shown the plot, and told the notes were rewritten: never what they say.
    assert "The captain comes" in logs[1]
    assert "ZQX-SECRET" not in "".join(logs) and "poisoned" not in "".join(logs)
