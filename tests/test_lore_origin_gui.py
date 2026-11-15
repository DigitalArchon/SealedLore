"""Where a lorebook's entries went, said by the window: the Lore page marks
each entry by where it came from, the dialog after a draft says what each of
the author's own characters brought, the preview marks their lore, the
premise dialog says their lorebook becomes the story's, and joining a story
says how many entries came."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.followup_dialog import FollowUpDialog  # noqa: E402
from sealedlore.gui.generate_dialog import GenerateDialog  # noqa: E402
from sealedlore.gui.lore_panel import LorePanel  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.window_followup import story_preview  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.providers.mock import MockChatProvider  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.test_card_link_gui import added_elena, keep_elena  # noqa: E402
from tests.test_draft_lorebooks import card, entry  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path):
    """A story open with Elena kept in Your characters, as the card link's
    tests have it."""
    root = tmp_path / "data"
    keep_elena(root, tmp_path)
    john = Character(id="john", name="John")
    story = Story(id="story-1", title="Doomsville", held_character_id="john")
    story.scene.present_character_ids = ["john"]
    save_story_bundle(StoryBundle(story=story, cast=[john]), root=root)
    window = MainWindow(root=root, use_mock=True)
    window.open_story("story-1")
    yield window
    window._private_allows_close = window._private_may_close = lambda: True
    window.close()


def test_the_lore_page_says_where_an_entry_came_from(app):
    panel = LorePanel()
    theirs = LoreEntry(title="Hellsville", content="Walled.", came_from="Elena's lorebook")
    ours = LoreEntry(title="The farm", content="North.")
    panel.set_entries([theirs, ours])
    panel.select_entry(theirs.id)
    assert not panel.came_from.isHidden()
    assert panel.came_from.text() == "From Elena's lorebook."
    assert panel.list.item(0).toolTip() == "From Elena's lorebook"
    assert panel.list.item(0).text() == "Hellsville  (from Elena's lorebook)"
    panel.select_entry(ours.id)
    assert panel.came_from.isHidden() and not panel.list.item(1).toolTip()
    assert panel.list.item(1).text() == "The farm"


def test_the_preview_tells_their_lore_from_the_drafts():
    bundle = StoryBundle(
        story=Story(title="Drafted"),
        lore=[
            LoreEntry(title="Hellsville", content="Walled.", came_from="Elena's *lorebook*"),
            LoreEntry(title="The farm", content="North."),
        ],
    )
    preview = story_preview(bundle)
    assert "### Hellsville\n\n*From Elena's \\*lorebook\\*.*\n\nWalled." in preview
    assert "### The farm\n\nNorth." in preview


def test_after_a_draft_the_dialog_says_what_they_brought_before_its_notes(app):
    dialog = FollowUpDialog(
        None,
        target="story",
        title="Drafted",
        go_on="Go on to Setup…",
        drafted=True,
        notes=["the draft's own lore entry “Hellsville” was left out"],
        brought=["Elena: their card as it is, in the cast; 1 entry of their lorebook…"],
    )
    text = dialog.log.toPlainText()
    assert text.index("From Your characters:") < text.index("Elena: their card as it is")
    assert text.index("Notes on the draft:") > text.index("Elena: their card")
    assert "Left out of the draft" not in text
    dialog.deleteLater()


def test_the_premise_dialog_says_their_lorebook_becomes_the_storys_lore(app):
    marta = card("Marta", [entry("Hellsville", "Walled."), entry("Off", "x", enabled=False)])
    dialog = GenerateDialog(MockChatProvider(), model="m", default_model="m", characters=[marta])
    assert dialog.with_label.text().startswith(
        "Marta, as their card has them, and their lorebook (1 entry) as the story's lore. "
    )
    dialog.characters = [Character(name="Ben")]
    dialog._show_characters()
    assert dialog.with_label.text().startswith("Ben, as their card has them. Say")
    dialog.deleteLater()


def test_joining_a_story_says_how_much_lore_came_and_where(app, window, monkeypatch):
    said: list = []
    monkeypatch.setattr(window.statusBar(), "showMessage", lambda text, *_a: said.append(text))
    added_elena(window, monkeypatch)
    assert said[-1].startswith(
        "Elena joined the cast, with 1 entry of their lorebook in the story's lore (the Lore page)."
    )
    assert window.session.bundle.lore[-1].came_from == "Elena's lorebook"
