"""A plot written by the model, in the window (gui/window_plot.py,
gui/plot_dialog.py, gui/plot_wishes.py): from Story → Write a plot with the
model…, the new story guide's last step and a premise draft, kept hidden
unless the author asks to see it. The model is the scripted mock."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from sealedlore.engine.plot_draft import PlotWishes  # noqa: E402
from sealedlore.gui.generate_dialog import GenerateDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.new_story_guide import NewStoryGuide  # noqa: E402
from sealedlore.gui.plot_dialog import (  # noqa: E402
    CONTINUE,
    EDIT,
    PlotDialog,
    PlotReadyDialog,
)
from sealedlore.gui.plot_wishes import DESCRIBED, MADE_UP, NONE, PlotWishesForm  # noqa: E402
from sealedlore.providers.base import ProviderError  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.test_authoring import DRAFT  # noqa: E402

PLOT = """\
# Characters

## Teodor Vask
- role: storyteller
- hidden: yes

A customs captain from the capital.

# Facts

- alarm: no — whether the keep is roused; set by events

# Events

## The captain comes
- pacing: any time
- brings: Teodor Vask
- sets: alarm = yes
- trigger: Someone is at the gate.

Teodor Vask rides up to the gate of Calder Keep.
"""


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


def wait_until(app: QApplication, condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.005)


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1920, 1040)
    window.show()
    window.open_story(story.id)
    settle(app)
    yield window
    window.close()


def model(window: MainWindow, monkeypatch, *replies: str) -> MockChatProvider:
    provider = MockChatProvider(list(replies))
    monkeypatch.setattr(window, "_current_provider", lambda: provider)
    return provider


def ready(monkeypatch, *, choice: str = CONTINUE, reveal: bool = False) -> list[dict]:
    """PlotReadyDialog answered as the author would; what it showed, kept."""
    seen: list[dict] = []

    def answer(dialog) -> int:
        seen.append(
            {
                "hidden": dialog.view.isHidden() and not dialog.revealed,
                "go_on": dialog.go_on_button.text(),
                "keep_off_shown": not dialog.keep_off.isHidden(),
                "keep_off": dialog.keep_off.isChecked(),
            }
        )
        if reveal:
            dialog.reveal()
            seen[-1]["plot"] = dialog.view.toPlainText()
            seen[-1]["notes"] = dialog.notes.text()
        dialog.choice = choice
        return QDialog.Accepted

    monkeypatch.setattr(PlotReadyDialog, "exec", answer)
    return seen


def test_the_story_menu_writes_a_plot_and_keeps_it_a_surprise(app, window, monkeypatch):
    provider = model(window, monkeypatch, PLOT)
    assert window.write_plot_action.isVisible() and window.write_plot_action.isEnabled()

    def ask(dialog) -> int:
        assert dialog.form.choice() == MADE_UP, "no 'no plot' here: that was asked already"
        assert NONE not in dialog.form.choices
        dialog.form.set_choice(DESCRIBED)
        dialog.form.premise.setPlainText("A captain comes looking for smugglers.")
        dialog.form.events.setCurrentIndex(dialog.form.events.findData("few"))
        dialog.form.kinds["mystery"].setChecked(True)
        return QDialog.Accepted

    monkeypatch.setattr(PlotDialog, "exec", ask)
    seen = ready(monkeypatch)
    window.write_plot_action.trigger()
    wait_until(app, lambda: seen)
    assert seen == [{"hidden": True, "go_on": "Close", "keep_off_shown": False, "keep_off": False}]
    session = window.session
    assert session.story.plot is not None
    vask = next(c for c in session.cast if c.name == "Teodor Vask")
    assert vask.plot_hidden
    # Kept a surprise: the Cast page doesn't show who the plot hides.
    assert "Teodor Vask" not in [
        window.cast_panel.list.item(i).text() for i in range(window.cast_panel.list.count())
    ]
    user = provider.requests[-1].messages[-1].text
    assert "A captain comes looking for smugglers." in user and "3 or 4" in user
    assert "mysteries, secrets and clues" in user
    assert window.right_tabs.isTabVisible(window.right_tabs.indexOf(window.plot_panel))


def test_shown_the_plot_and_its_notes_once_asked(app, window, monkeypatch):
    broken = PLOT + "\n## Odd\n- requires: tide = high\n\nNothing.\n"
    model(window, monkeypatch, broken, broken)
    monkeypatch.setattr(PlotDialog, "exec", lambda dialog: QDialog.Accepted)
    window.config.show_plot_spoilers = True
    seen = ready(monkeypatch, reveal=True)
    window.write_plot_action.trigger()
    wait_until(app, lambda: seen)
    shown = seen[0]
    assert shown["hidden"], "hidden until asked, even with spoilers on"
    assert shown["keep_off_shown"] and shown["keep_off"] is True
    assert "The captain comes" in shown["plot"] and "Teodor Vask" in shown["plot"]
    assert "Odd" in shown["notes"] and "tide" in shown["notes"]
    # Looked at: spoilers stay as they were.
    assert window.config.show_plot_spoilers


def test_keeping_it_a_surprise_turns_the_plot_page_s_spoilers_off(app, window, monkeypatch):
    model(window, monkeypatch, PLOT)
    monkeypatch.setattr(PlotDialog, "exec", lambda dialog: QDialog.Accepted)
    window.config.show_plot_spoilers = True
    seen = ready(monkeypatch)
    window.write_plot_action.trigger()
    wait_until(app, lambda: seen)
    settle(app)
    assert not window.config.show_plot_spoilers


def test_edit_in_the_plot_editor_opens_the_story_s_plot(app, window, monkeypatch):
    model(window, monkeypatch, PLOT)
    monkeypatch.setattr(PlotDialog, "exec", lambda dialog: QDialog.Accepted)
    seen = ready(monkeypatch, choice=EDIT)
    window.write_plot_action.trigger()
    wait_until(app, lambda: seen)
    wait_until(app, lambda: window._plot_editor is not None and window._plot_editor.isVisible())
    editor = window._plot_editor
    assert editor.story.id == window.session.story.id
    assert [e.title for e in editor.document.events] == ["The captain comes"]
    editor.close()


def test_the_plot_editor_open_on_the_story_follows_a_plot_the_model_writes(
    app, window, monkeypatch
):
    window.edit_plot_action.trigger()
    editor = window._plot_editor
    assert editor.story is not None and editor.document.events == []
    model(window, monkeypatch, PLOT)
    monkeypatch.setattr(PlotDialog, "exec", lambda dialog: QDialog.Accepted)
    seen = ready(monkeypatch)
    window.write_plot_action.trigger()
    wait_until(app, lambda: seen)
    assert [e.title for e in editor.document.events] == ["The captain comes"]
    assert not editor.modified
    editor.close()


def test_a_failed_plot_can_be_tried_again_or_left(app, window, monkeypatch):
    model(window, monkeypatch, "Not a plot at all.")
    monkeypatch.setattr(PlotDialog, "exec", lambda dialog: QDialog.Accepted)
    asked: list[str] = []

    def leave(box) -> int:
        asked.append(box.informativeText())
        box.clickedButton = lambda: None  # neither: closed
        return 0

    monkeypatch.setattr(QMessageBox, "exec", leave)
    window.write_plot_action.trigger()
    wait_until(app, lambda: asked)
    assert "no event" in asked[0]
    assert window.session.story.plot is None
    window.session.provider = MockChatProvider([])
    provider = model(window, monkeypatch)
    provider.error = ProviderError("HTTP 503: the endpoint is down")
    asked.clear()
    window.write_plot_action.trigger()
    wait_until(app, lambda: asked)
    assert "503" in asked[0]


def test_a_stopped_plot_changes_nothing_and_says_so(app, window, monkeypatch):
    provider = model(window, monkeypatch, PLOT)
    provider.delay = 0.05  # each chunk: time to press Stop
    monkeypatch.setattr(PlotDialog, "exec", lambda dialog: QDialog.Accepted)
    monkeypatch.setattr(QMessageBox, "exec", lambda box: pytest.fail(f"asked: {box.text()}"))
    window.write_plot_action.trigger()
    wait_until(app, lambda: window._busy_dialog is not None)
    window._busy_dialog.canceled.emit()
    wait_until(app, lambda: not window._busy)
    settle(app)
    assert window.session.story.plot is None
    assert "No plot was written" in window.statusBar().currentMessage()


def test_the_plot_items_are_a_story_s_alone(app, window):
    window.session.story.mode = "chat"
    window._update_controls()
    assert not window.write_plot_action.isVisible()
    assert not window.edit_plot_action.isVisible()
    window.session.story.mode = "story"
    window._update_controls()
    assert window.write_plot_action.isVisible()


# --- a new story ------------------------------------------------------------------------


def test_the_guide_s_last_step_asks_for_a_plot_written_before_start(app, window, monkeypatch):
    model(window, monkeypatch, PLOT)
    started: list[bool] = []
    monkeypatch.setattr(
        window, "start_story", lambda: started.append(bool(window.session.story.plot))
    )
    seen = ready(monkeypatch)

    def fill(guide) -> int:
        guide.title.setText("Calder Keep")
        guide.world.setPlainText("A keep on the cliffs, empty for a hundred years.")
        guide.next_button.click()
        window.cast_panel.name.setText("Serrik Vaun")
        guide.next_button.click()
        guide.next_button.click()
        guide.next_button.click()
        assert guide.stack.currentIndex() == 4 and guide.next_button.text() == "Create the story"
        assert guide.plot.choice() == NONE, "no plot unless one is asked for"
        assert guide.plot_wishes is None
        guide.plot.set_choice(MADE_UP)
        guide.next_button.click()
        return guide.result()

    monkeypatch.setattr(NewStoryGuide, "exec", fill)
    window.new_story()
    wait_until(app, lambda: started)
    assert seen and seen[0]["go_on"] == "Start the story…"
    assert started == [True], "Start follows the plot, with it in"


def test_a_guide_with_no_plot_goes_straight_to_start(app, window, monkeypatch):
    provider = model(window, monkeypatch, PLOT)
    started: list[bool] = []
    monkeypatch.setattr(window, "start_story", lambda: started.append(True))

    def fill(guide) -> int:
        guide.title.setText("No Plot Here")
        guide.next_button.click()
        window.cast_panel.name.setText("Ada")
        assert guide._finish()
        guide.done(NewStoryGuide.Accepted)
        return NewStoryGuide.Accepted

    monkeypatch.setattr(NewStoryGuide, "exec", fill)
    window.new_story()
    settle(app)
    assert started == [True] and provider.requests == []
    assert window.session.story.plot is None


def test_a_premise_draft_s_plot_is_written_after_setup_and_before_start(app, window, monkeypatch):
    plot = PLOT.replace("Calder Keep", "the east gate")
    provider = model(window, monkeypatch, json.dumps(DRAFT), plot)
    order: list[str] = []
    monkeypatch.setattr(window, "open_setup", lambda: order.append("setup") or True)
    monkeypatch.setattr(
        window, "start_story", lambda: order.append(f"start:{bool(window.session.story.plot)}")
    )
    seen = ready(monkeypatch)

    def draft(dialog) -> int:
        assert dialog.plot.choice() == NONE
        dialog.premise.setPlainText("Winter at a besieged town.")
        dialog.plot.set_choice(MADE_UP)
        dialog._generate()
        wait_until(app, lambda: dialog._thread is None)
        return dialog.result()

    monkeypatch.setattr(GenerateDialog, "exec", draft)
    window.new_story_from_premise()
    wait_until(app, lambda: len(order) == 2)
    assert order == ["setup", "start:True"]
    assert seen
    assert window.session.story.title == "Winter at Hellsville"
    assert len(provider.requests) == 2


def test_the_wishes_form_says_only_what_was_chosen(app):
    form = PlotWishesForm(offer_none=True)
    form.show()
    assert form.wishes() is None and form.details.isHidden()
    form.set_choice(MADE_UP)
    assert form.wishes() == form.wishes().__class__()
    form.set_choice(DESCRIBED)
    assert not form.details.isHidden()
    form.premise.setPlainText("  A heist goes wrong.  ")
    form.spacing.setCurrentIndex(form.spacing.findData("spread"))
    form.disruption.setCurrentIndex(form.disruption.findData("upending"))
    form.kinds["danger"].setChecked(True)
    form.newcomers.setCurrentIndex(form.newcomers.findData("none"))
    wishes = form.wishes()
    assert (wishes.premise, wishes.spacing, wishes.disruption, wishes.kinds) == (
        "A heist goes wrong.",
        "spread",
        "upending",
        ("danger",),
    )
    assert wishes.newcomers == "none" and wishes.events == "" and wishes.scale == ""
    form.deleteLater()


def test_the_plot_form_asks_how_many_newcomers_and_how_much_about_each(app):
    """The author (Nov 6 2026): a few new people or lots, briefly or in
    depth. It was a tick box, "It may bring in new people and places"."""

    def choose(combo, value: str) -> None:
        combo.setCurrentIndex(combo.findData(value))
        assert combo.currentData() == value

    form = PlotWishesForm(offer_none=False)
    form.show()
    form.set_choice(DESCRIBED)
    assert (form.wishes().newcomers, form.wishes().depth) == ("", "")
    choose(form.newcomers, "several")
    choose(form.depth, "deep")
    assert (form.wishes().newcomers, form.wishes().depth) == ("several", "deep")
    # Nobody new: how much about each means nothing, and is put away.
    choose(form.newcomers, "none")
    assert not form.depth.isEnabled()
    assert (form.wishes().newcomers, form.wishes().depth) == ("none", "")
    choose(form.newcomers, "many")
    assert form.depth.isEnabled() and form.wishes().depth == "deep"
    # Read only while a draft is written, and as it was after.
    form.set_read_only(True)
    assert not form.newcomers.isEnabled() and not form.depth.isEnabled()
    form.set_read_only(False)
    assert form.newcomers.isEnabled() and form.depth.isEnabled()
    choose(form.newcomers, "none")
    form.set_read_only(True)
    form.set_read_only(False)
    assert form.newcomers.isEnabled() and not form.depth.isEnabled()
    # Made up from the story alone: nothing of the details is asked.
    form.set_choice(MADE_UP)
    assert form.wishes() == PlotWishes()
    form.deleteLater()
