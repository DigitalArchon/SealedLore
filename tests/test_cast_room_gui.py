"""The cast's room in the prompt, in the window, offscreen: Settings →
Context sets it, the Cast page says how the cards fit and shows each card's
condensed text, and condensing runs and is taken in (the scripted mock
condenses inline, so a poll finds it done)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.card_condense import fingerprint  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.config import Config, ProviderConfig  # noqa: E402
from sealedlore.models.supporting import SupportingCast  # noqa: E402
from sealedlore.providers.base import ProviderError  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402

JANE = "Jane keeps the farmhouse stove going through the long winter. " * 90


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_settings_set_the_room_as_a_share_or_a_fixed_number(app, story):
    from sealedlore.gui.settings_dialog import SettingsDialog

    config = Config(
        providers=[ProviderConfig(name="nano", base_url="https://nano-gpt.com/api/v1", model="m")],
        active_provider_name="nano",
    )
    dialog = SettingsDialog(config, story)
    assert dialog.cast_limit.currentData() == "share"
    assert dialog.cast_share.value() == 15
    # Its page isn't the one showing: hidden by the choice, not by the page.
    assert dialog.cast_tokens.isHidden() and not dialog.cast_share.isHidden()
    dialog.cast_share.setValue(25)
    dialog._save()
    assert config.cast_limit == "share" and config.cast_share == 0.25

    dialog = SettingsDialog(config, story)
    dialog.cast_limit.setCurrentIndex(dialog.cast_limit.findData("tokens"))
    assert not dialog.cast_tokens.isHidden() and dialog.cast_share.isHidden()
    dialog.cast_tokens.setValue(12_000)
    dialog._save()
    assert config.cast_limit == "tokens" and config.cast_cap(60_000) == 12_000


@pytest.fixture
def window(app, tmp_path: Path, story) -> MainWindow:
    cast = [
        Character(id="char-john", name="John Jacobs", summary="A survivor who walks alone."),
        Character(
            id="char-jane", name="Jane Jacobs", summary="John's wife.", full_description=JANE
        ),
    ]
    story.held_character_id = "char-john"
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.suggest_characters = False
    window.config.scene_reads = "manual"
    window.config.cast_limit = "tokens"
    window.config.cast_token_cap = 500
    window._provider = MockChatProvider(["Jane: John's steady, practical wife."])
    window.open_story(story.id)
    window.show_cast_page()
    return window


def test_the_cast_page_says_the_cast_is_over_its_room_and_condenses_it(app, window: MainWindow):
    panel = window.cast_panel
    assert panel.room_banner.isVisibleTo(window)
    text = panel.room_banner.message.text()
    assert "more than their room of 500 (a fixed 500 tokens)" in text
    assert "Jane Jacobs" in text and "sent whole until then" in text

    window._condense_soon.stop()
    window._condense_if_needed()
    window._poll_condensing()
    jane = next(c for c in window.session.cast if c.id == "char-jane")
    assert jane.condensed == "Jane: John's steady, practical wife."
    text = panel.room_banner.message.text()
    assert "Sent condensed: Jane Jacobs." in text
    panel.select_character("char-jane")
    assert panel.condensed.toPlainText() == jane.condensed
    assert "Sent in place of the description" in panel.condensed_note.text()


def test_the_author_edits_a_condensed_card_and_keeps_a_card_whole(app, window: MainWindow):
    panel = window.cast_panel
    panel.select_character("char-jane")
    jane = panel.current
    panel.condensed.setPlainText("Jane, in my words.")
    assert jane.condensed == "Jane, in my words."
    assert jane.condensed_from == fingerprint(jane)
    panel.keep_whole.setChecked(True)
    assert jane.keep_whole is True
    window._refresh_cast_room()
    assert window.session.cast_fit().forms["char-jane"] == "whole"
    assert "always sent whole" in panel.condensed_note.text()
    assert "isn't used" in panel.condensed_note.text()
    # Her text stays in view, but there is nothing to condense it for.
    assert panel.condensed_box.isVisibleTo(window)
    assert not panel.condense_button.isVisibleTo(window)
    # John's short card is sent whole while Jane's is condensed, and can be
    # condensed all the same.
    panel.select_character("char-john")
    assert panel.condensed_box.isVisibleTo(window)
    assert panel.condense_button.isVisibleTo(window)
    assert panel.condense_button.text() == "Condense this card"
    assert "condensing the other cards" in panel.condensed_note.text()


def test_the_authors_own_condensed_card_ends_a_failed_ones_note(app, window: MainWindow):
    """The panel kept the text itself, as the session's `set_condensed` does
    but without taking the failure off, and a failure stands while the card's
    fingerprint (which leaves the condensed text out) is unchanged: the card
    said "Couldn't be written" over the author's own words."""
    window.session.provider.error = ProviderError("the model was out")
    window._condense_soon.stop()
    window._condense_if_needed()
    window._poll_condensing()
    panel = window.cast_panel
    panel.select_character("char-jane")
    assert "Couldn't be written (the model was out)" in panel.condensed_note.text()
    # Blank, it still couldn't be written, and isn't tried again unasked.
    panel.condensed.setPlainText(" ")
    assert window.session.condense_failures() == {"char-jane": "the model was out"}
    assert "Couldn't be written" in panel.condensed_note.text()
    panel.condensed.setPlainText("Jane, in my words.")
    jane = panel.current
    assert jane.condensed == "Jane, in my words." and jane.condensed_from == fingerprint(jane)
    assert window.session.condense_failures() == {}
    assert "Couldn't be written" not in panel.condensed_note.text()
    assert "Sent in place of the description" in panel.condensed_note.text()
    assert "Couldn't be condensed" not in panel.room_banner.message.text()


def test_a_cast_that_fits_says_nothing(app, window: MainWindow):
    window.config.cast_token_cap = 100_000
    window._refresh_cast_room()
    assert not window.cast_panel.room_banner.isVisibleTo(window)


def test_a_card_can_be_condensed_while_the_cast_fits_unless_kept_whole(app, window: MainWindow):
    """The Condensed row used to show only with a note to give: while the
    cast fit, it appeared on a card only once Always send the whole card was
    ticked, the one setting that makes condensing it pointless."""
    window.config.cast_token_cap = 100_000
    window._refresh_cast_room()
    panel = window.cast_panel
    panel.select_character("char-jane")
    assert panel.condensed_box.isVisibleTo(window)
    assert panel.condense_button.isVisibleTo(window)
    assert "fit their room whole" in panel.condensed_note.text()

    panel.keep_whole.setChecked(True)
    # At once, before the room is worked out again.
    assert not panel.condensed_box.isVisibleTo(window)
    panel.keep_whole.setChecked(False)
    assert panel.condense_button.isVisibleTo(window)

    window._condense_soon.stop()
    panel.condense_button.click()
    window._poll_condensing()
    jane = next(c for c in window.session.cast if c.id == "char-jane")
    assert jane.condensed == "Jane: John's steady, practical wife."
    assert panel.condense_button.text() == "Write it again"


def test_a_supporting_cards_condense_button_condenses_it(app, tmp_path: Path, story):
    """The window looked for the card in the main cast alone, so a
    supporting card's Write it again did nothing."""
    cast = [Character(id="char-john", name="John Jacobs", summary="A survivor.")]
    supporting = [Character(id="char-ben", name="Ben", summary="A trader.", full_description=JANE)]
    story.held_character_id = "char-john"
    bundle = StoryBundle(story=story, cast=cast, supporting=SupportingCast(characters=supporting))
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.config.suggest_characters = False
    window.config.scene_reads = "manual"
    window._provider = MockChatProvider(["Ben: trades in salvage."])
    window.open_story(story.id)
    window.show_cast_page()
    window._condense_soon.stop()
    panel = window.cast_panel
    panel.select_character("char-ben")
    assert panel.condensed_box.isVisibleTo(window)
    panel.condense_button.click()
    window._poll_condensing()
    ben = next(c for c in window.session.supporting if c.id == "char-ben")
    assert ben.condensed == "Ben: trades in salvage."
