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

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

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


def test_the_premise_dialog_says_the_whole_lorebook_comes_and_what_may_be_off(app):
    """A story drafted around a character is made from their card: every
    entry comes (the drafter reads them all), and the dialog says so, with
    what a premise that moves them may switch off."""
    marta = card("Marta", [entry("Hellsville", "Walled."), entry("Off", "x", enabled=False)])
    dialog = GenerateDialog(MockChatProvider(), model="m", default_model="m", characters=[marta])
    assert dialog.with_label.text() == (
        "Marta, as their card has them, and every entry of their lorebook (2) as the story's "
        "lore. The draft reads all of it and builds the story to agree; any entry your premise "
        "leaves no place for (it sets them somewhere else) comes in switched off, for you to "
        "switch on. Say in the premise what kind of story, in what world, and whom you play."
    )
    dialog.characters = [Character(name="Ben")]
    dialog._show_characters()
    assert dialog.with_label.text().startswith("Ben, as their card has them. Say")
    dialog.deleteLater()


def test_joining_a_story_leaves_their_lorebook_out_and_says_why(app, window, monkeypatch):
    """The author, Nov 15 2026: a character joining a story that exists
    (Doomsville, say) mustn't bring their world's lore, which "breaks the
    game"; and the author must know what stays out and why."""
    said: list = []
    monkeypatch.setattr(window.statusBar(), "showMessage", lambda text, *_a: said.append(text))
    added_elena(window, monkeypatch)
    assert said[-1] == (
        "Elena joined the cast. Their lorebook (1 entry) stayed out of the story's lore, as it "
        "is about their own world."
    )
    assert window.session.bundle.lore == []


def test_ticked_in_their_lorebook_comes_and_says_where(app, window, monkeypatch):
    said: list = []
    monkeypatch.setattr(window.statusBar(), "showMessage", lambda text, *_a: said.append(text))
    added_elena(window, monkeypatch, with_lore=True)
    assert said[-1].startswith(
        "Elena joined the cast, with 1 entry of their lorebook in the story's lore (the Lore page)."
    )
    assert window.session.bundle.lore[-1].came_from == "Elena's lorebook"


def test_the_picker_says_what_stays_out_and_why_and_what_ticking_does(app, window):
    from sealedlore.gui.collection_picker import CollectionPicker

    picker = CollectionPicker(
        window._kept(), window._collection_pictures, title="t", lead="l", action="Add"
    )
    picker.show()
    assert picker.lore_words.isHidden(), "nobody chosen yet"
    picker.list.item(0).setCheckState(Qt.Checked)
    assert not picker.with_lore.isChecked()
    assert picker.with_lore.text() == "Bring their lorebook too"
    words = picker.lore_words.text()
    assert words.startswith("Elena's lorebook (1 entry) stays out of this story.")
    assert "would clash with the world this one already has" in words
    assert "their card and pictures come in either way" in words
    picker.with_lore.setChecked(True)
    assert picker.lore_words.text().startswith(
        "Elena's lorebook (1 entry) will go into this story's lore, and the storyteller will "
        "take every entry as true of this story."
    )
    assert picker.options() == (True, True)
    picker.deleteLater()


def test_a_card_into_the_open_story_is_asked_with_the_lorebook_out(app, window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    seen: list = []

    def exec_(box) -> int:
        seen.append((box.checkBox().isChecked(), box.checkBox().text(), box.informativeText()))
        box.checkBox().setChecked(True)
        seen.append(box.informativeText())
        return 0

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    assert window._ask_where_card_goes(Character(name="Ann"), 3) is None
    ticked, label, words = seen[0]
    assert (ticked, label) == (False, "Bring their lorebook too")
    assert "Ann's lorebook (3 entries) stays out of this story." in words
    assert "Ann's lorebook (3 entries) will go into this story's lore" in seen[1]


def test_a_story_from_a_card_says_its_whole_lorebook_comes(app, tmp_path):
    from sealedlore.gui.tavern_dialog import CardImportDialog
    from sealedlore.storage.tavern_cards import read_card
    from tests.test_tavern import ELENA, png_card, v2

    fields = dict(ELENA)
    book = [*ELENA["character_book"]["entries"], {**ELENA["character_book"]["entries"][0]}]
    book[1].update(comment="The old well", keys=["well"], enabled=False)
    fields["character_book"] = {"entries": book}
    dialog = CardImportDialog(read_card(png_card(tmp_path, {"chara": v2(fields)})))
    rows = [label.text() for label in dialog.findChildren(QLabel)]
    assert (
        "Every entry of the card's lorebook (2) becomes the story's lore (1 switched off on "
        "the card stays off): the story is set in the card's own world, so the storyteller "
        "takes them as true."
    ) in rows
    dialog.deleteLater()
