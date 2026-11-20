"""Story → Suggestions from the story… (gui/story_suggestions_dialog.py,
gui/window_story_suggestions.py). Offscreen, no network."""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from sealedlore.gui.story_suggestions_dialog import NONE_YET, StorySuggestionsDialog  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.models.story_suggestion import StorySuggestion  # noqa: E402
from tests.test_images_gui import window  # noqa: E402,F401  (the window fixture)

OLD = "The keep stands on a hill above the river, its walls grey and its gates long gone."


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def suggestions() -> list[StorySuggestion]:
    return [
        StorySuggestion(
            kind="lore_new",
            title="The Ford",
            keywords=["ford"],
            content="Shallow.",
            why="They crossed it.",
        ),
        StorySuggestion(
            kind="lore_change", target_id="l-keep", title="Calder Keep", content="Burned."
        ),
        StorySuggestion(
            kind="card_change", target_id="c1", title="Serrik Vaun", content="He lost two fingers."
        ),
    ]


def labels(widget) -> str:
    return "\n".join(label.text() for label in widget.findChildren(QLabel))


def test_each_suggestion_shows_what_it_rests_on_and_applies_as_edited(app):
    waiting = suggestions()
    applied: list[tuple[str, dict]] = []
    declined: list[str] = []

    def apply(suggestion, values):
        applied.append((suggestion.title, values))
        waiting.remove(suggestion)
        return f"Added the lore entry {values.get('title', suggestion.title)}."

    def decline(suggestion):
        declined.append(suggestion.title)
        waiting.remove(suggestion)

    dialog = StorySuggestionsDialog(
        lambda: list(waiting),
        current=lambda s: OLD if s.kind == "lore_change" else "",
        apply=apply,
        decline=decline,
        look_over=None,
    )
    new, change, card = dialog.cards
    text = labels(dialog)
    assert "From the story: They crossed it." in text
    assert "keeps less than half of the entry's words" in labels(change)  # Burned. loses it all
    assert [c.apply_button.text() for c in dialog.cards] == [
        "Add to the lore",
        "Change the entry",
        "Add to their card",
    ]
    new.title_field.setText("The Ford at Calder")
    new.keywords_field.setText("ford, crossing")
    new.apply_button.click()
    assert applied == [
        (
            "The Ford",
            {
                "content": "Shallow.",
                "title": "The Ford at Calder",
                "keywords": ["ford", "crossing"],
            },
        )
    ]
    assert dialog.said.text() == "Added the lore entry The Ford at Calder."
    dialog.cards[1].decline_button.click()  # the card change, now second
    assert declined == ["Serrik Vaun"] and len(dialog.cards) == 1
    assert not dialog.look_button.isEnabled()  # no Look over offered
    dialog.deleteLater()


def test_with_nothing_waiting_it_says_what_will_come(app):
    dialog = StorySuggestionsDialog(
        list,
        current=lambda s: "",
        apply=lambda s, v: None,
        decline=lambda s: None,
        look_over=lambda: None,
    )
    assert NONE_YET in labels(dialog) and dialog.look_button.isEnabled()
    dialog.set_busy(True)
    assert not dialog.look_button.isEnabled()
    dialog.deleteLater()


def test_the_window_applies_and_counts_them(app, window):  # noqa: F811
    session = window.session
    session.bundle.lore.append(LoreEntry(id="l-keep", title="Calder Keep", content=OLD))
    session.story.story_suggestions.extend(
        [
            StorySuggestion(
                kind="lore_change",
                target_id="l-keep",
                title="Calder Keep",
                content="A burned keep.",
            ),
            StorySuggestion(kind="lore_new", title="The Ford", content="Shallow."),
        ]
    )
    window._update_suggestions_action()
    assert window.story_suggestions_action.text().endswith("(2)")
    change, new = session.story_suggestions
    assert window._suggestion_current(change) == OLD
    said = window._apply_story_suggestion(change, {"content": "A burned keep, empty."})
    assert said == "Changed the lore entry Calder Keep."
    assert session.bundle.lore[-1].content == "A burned keep, empty."
    assert window.story_suggestions_action.text().endswith("(1)")
    window._decline_story_suggestion(new)
    assert not window.story_suggestions_action.text().endswith(")")
    # Gone meanwhile: said, not raised.
    gone = StorySuggestion(kind="lore_change", target_id="nowhere", title="X", content="y")
    session.story.story_suggestions.append(gone)
    assert window._apply_story_suggestion(gone, {}).startswith("⚠ Not applied")
