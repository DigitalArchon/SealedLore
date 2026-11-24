"""What is typed on the Cast, Scene, Lore and Style pages is in the story at
once and saved a moment after the last key (gui/window_edits.py). Every key
used to save the whole story and assemble the prompt again on the GUI thread.
Offscreen, the mock provider only."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.window_collection import SAVE_DELAY_MS  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
from tests.conftest import make_exchange  # noqa: E402

TYPED = " Limps since the siege."


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def open_window(root: Path, story, cast) -> MainWindow:
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    bundle.lore = [LoreEntry(id="lore-keep", title="Calder Keep", content="Empty for a century.")]
    save_story_bundle(bundle, root=root)
    window = MainWindow(root=root, use_mock=True)
    window.resize(1366, 768)
    window.show()
    window.open_story(story.id)
    return window


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    window = open_window(tmp_path, story, cast)
    yield window
    window.close()


def count_saves(window: MainWindow, monkeypatch) -> list[int]:
    session = window.session
    saves: list[int] = []
    real = session.save
    monkeypatch.setattr(session, "save", lambda: saves.append(1) or real())
    return saves


def count_prompts(window: MainWindow, monkeypatch) -> list[int]:
    session = window.session
    assembled: list[int] = []
    real = session.assemble
    monkeypatch.setattr(session, "assemble", lambda *a, **k: assembled.append(1) or real(*a, **k))
    return assembled


def on_disk(window: MainWindow) -> StoryBundle:
    return load_story_bundle(window.session.story.id, root=window.root)


def wait_for(app: QApplication, condition, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.005)


def past_the_delay(app: QApplication) -> None:
    QTest.qWait(SAVE_DELAY_MS + 150)
    app.processEvents()


def test_typing_in_a_card_saves_once_a_moment_after_the_last_key(app, window, monkeypatch):
    window.show_cast_board(True, "char-serrik", card=True)
    panel = window.cast_panel
    serrik = window.session.cast[0]
    before = serrik.summary
    saves = count_saves(window, monkeypatch)
    prompts = count_prompts(window, monkeypatch)
    field = panel.summary
    field.setFocus()
    field.moveCursor(field.textCursor().MoveOperation.End)
    QTest.keyClicks(field, TYPED)
    # The card has every key at once; nothing is saved or assembled per key.
    assert serrik.summary == before + TYPED
    assert saves == [] and prompts == []
    assert on_disk(window).cast[0].summary == before
    past_the_delay(app)
    assert saves == [1], "one save, a moment after the last key"
    assert len(prompts) == 1
    assert on_disk(window).cast[0].summary == before + TYPED


def test_their_own_models_name_is_saved_a_moment_after_it_is_typed(app, window, monkeypatch):
    from sealedlore.gui.character_roles import OWN

    window.show_cast_board(True, "char-maela", card=True)
    played_by = window.cast_panel.played_by
    played_by.combo.setCurrentIndex(played_by.combo.findData(OWN))
    saves = count_saves(window, monkeypatch)
    for typed in range(1, len("Local::maela") + 1):  # a key at a time
        played_by.model.setText("Local::maela"[:typed])
    maela = window.session.cast[1]
    assert maela.own_model == "Local::maela" and saves == []
    past_the_delay(app)
    assert saves == [1]
    assert on_disk(window).cast[1].own_model == "Local::maela"


def test_a_condensed_text_typed_is_saved_a_moment_after_the_last_key(app, window, monkeypatch):
    """The card's and the entry's own condensed words, kept by the session
    (`set_condensed`, `set_condensed_lore`) at every key and saved once."""
    window.show_cast_board(True, "char-serrik", card=True)
    saves = count_saves(window, monkeypatch)
    words = "Serrik, in my words."
    for typed in range(1, len(words) + 1):  # a key at a time
        window.cast_panel.condensed.setPlainText(words[:typed])
    assert window.session.cast[0].condensed == words and saves == []
    past_the_delay(app)
    assert saves == [1]
    assert on_disk(window).cast[0].condensed == words
    window.show_cast_board(False)
    window.lore_panel.select_entry("lore-keep")
    del saves[:]
    words = "The Keep, briefly."
    for typed in range(1, len(words) + 1):
        window.lore_panel.condensed.setPlainText(words[:typed])
    assert window.session.bundle.lore[0].condensed_now() == words and saves == []
    past_the_delay(app)
    assert saves == [1]
    assert on_disk(window).lore[0].condensed_now() == words


def test_a_choice_made_is_kept_at_once_with_what_was_typed_before_it(app, window, monkeypatch):
    window.right_tabs.setCurrentWidget(window.lore_panel)
    panel = window.lore_panel
    panel.select_entry("lore-keep")
    saves = count_saves(window, monkeypatch)
    QTest.keyClicks(panel.keywords, "keep")
    assert saves == []
    panel.always_on.setChecked(True)
    assert saves == [1]
    entry = on_disk(window).lore[0]
    assert entry.keywords == ["keep"] and entry.always_on
    past_the_delay(app)
    assert saves == [1], "nothing left waiting to be saved again"


def test_a_turn_sent_before_the_save_goes_with_what_was_typed(app, window, monkeypatch):
    window.right_tabs.setCurrentWidget(window.scene_panel)
    field = window.scene_panel.location
    field.selectAll()
    QTest.keyClicks(field, "The well chamber")
    session = window.session
    # The scene is stamped on the passage at once: a branch gone to and
    # back, or the next read, starts from what was typed.
    assert session.path()[-1].meta.scene.location == "The well chamber"
    provider = MockChatProvider(["Water drips somewhere below.", "{}"])
    window._provider = provider
    window.composer.input.setPlainText("I listen.")
    window.send_turn()
    # Saved as the job started, before the worker became the story's writer.
    assert on_disk(window).story.scene.location == "The well chamber"
    assert not window._edits_save.isActive()
    wait_for(app, lambda: not window._busy)
    sent = "\n".join(message.text for message in provider.requests[0].messages)
    assert "The well chamber" in sent


def test_closing_keeps_what_was_typed_a_moment_ago(app, tmp_path, story, cast):
    window = open_window(tmp_path, story, cast)
    window.right_tabs.setCurrentWidget(window.style_panel)
    QTest.keyClicks(window.style_panel.notes, "Keep it grim.")
    assert window._edits_save.isActive()
    window.close()
    assert not window._edits_save.isActive()
    assert load_story_bundle(story.id, root=tmp_path).story.style.notes == "Keep it grim."


def test_the_same_story_opened_again_has_what_was_typed(app, window):
    window.right_tabs.setCurrentWidget(window.lore_panel)
    panel = window.lore_panel
    panel.select_entry("lore-keep")
    panel.content.moveCursor(panel.content.textCursor().MoveOperation.End)
    QTest.keyClicks(panel.content, " Its well still runs.")
    # Opened from disk at once: the edit was there, not a moment behind.
    window.open_story(window.session.story.id)
    assert window.session.bundle.lore[0].content == "Empty for a century. Its well still runs."


def test_the_prompt_page_shows_what_was_typed_when_it_is_shown(app, window, monkeypatch):
    window.right_tabs.setCurrentWidget(window.style_panel)
    saves = count_saves(window, monkeypatch)
    QTest.keyClicks(window.style_panel.notes, "Keep it grim.")
    assert saves == []
    window.show_prompt_page(True)
    assert saves == [1] and not window._edits_save.isActive()
    assert "Keep it grim." in window.inspector.prompt_text()
