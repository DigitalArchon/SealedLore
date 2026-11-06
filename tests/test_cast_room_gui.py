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
    # John's short card has nothing condensed and needs nothing.
    panel.select_character("char-john")
    assert not panel.condensed_box.isVisibleTo(window)


def test_a_cast_that_fits_says_nothing(app, window: MainWindow):
    window.config.cast_token_cap = 100_000
    window._refresh_cast_room()
    assert not window.cast_panel.room_banner.isVisibleTo(window)
