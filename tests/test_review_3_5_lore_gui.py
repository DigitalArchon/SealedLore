"""What the review of 3.5.0b1 found in lore and placeholders, the window's
side (Nov 22 2026): each test fails on 0f289da."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.placeholders import names_for  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402

SERRIK, MAELA, IDRIS = "char-serrik", "char-maela", "char-idris"


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


# --- L5: Setup and a {{char}} or {{user}} it doesn't list --------------------------


def test_setup_keeps_a_char_who_is_a_supporting_character(app, story, cast):
    """A story made from a card, its character moved to the supporting
    cards: any Save of Setup made {{char}} nobody ("the character keeps the
    only working radio")."""
    from sealedlore.gui.setup_dialog import SetupDialog

    story.world_bible = "{{char}} keeps the only working radio, and {{user}} needs it."
    story.setup.placeholders.user_id = SERRIK
    story.setup.placeholders.char_id = IDRIS
    main, supporting = [c for c in cast if c.id != IDRIS], [c for c in cast if c.id == IDRIS]
    dialog = SetupDialog(story, main, supporting=supporting)
    assert dialog.char_is.currentText() == "Captain Idris"  # offered, and shown as it is
    assert dialog.user_is.findData(IDRIS) == -1  # nobody plays a supporting character
    dialog._save()
    assert story.setup.placeholders.char_id == IDRIS
    assert names_for(story, cast).fill(story.world_bible).startswith("Captain Idris keeps")
    dialog.deleteLater()


def test_setup_leaves_an_id_it_did_not_offer_as_it_was(app, story, cast):
    """Someone the plot keeps hidden isn't listed (a spoiler), and a caller
    that names no supporting cards lists none: what they are set to stays,
    under a line that names nobody, until the author chooses another."""
    from sealedlore.gui.setup_dialog import PLACEHOLDER_KEPT, SetupDialog

    story.setup.placeholders.user_id = IDRIS  # a supporting card, here unknown to Setup
    story.setup.placeholders.char_id = MAELA  # hidden by the plot
    main = [c for c in cast if c.id != IDRIS]
    dialog = SetupDialog(story, main, hidden=frozenset({MAELA}))
    assert dialog.char_is.currentText() == PLACEHOLDER_KEPT
    assert dialog.user_is.currentText() == PLACEHOLDER_KEPT
    assert "Maela" not in " ".join(
        dialog.char_is.itemText(index) for index in range(dialog.char_is.count())
    )
    assert not dialog.edited()
    dialog._save()
    assert story.setup.placeholders.user_id == IDRIS
    assert story.setup.placeholders.char_id == MAELA
    # Chosen anew, it is what was chosen.
    dialog = SetupDialog(story, main, hidden=frozenset({MAELA}))
    dialog.char_is.setCurrentIndex(dialog.char_is.findData(SERRIK))
    dialog.user_is.setCurrentIndex(0)
    dialog._save()
    assert story.setup.placeholders.char_id == SERRIK
    assert story.setup.placeholders.user_id is None
    dialog.deleteLater()


def test_setup_still_lets_go_of_someone_removed_while_it_was_open(app, story, cast):
    from sealedlore.gui.setup_dialog import SetupDialog

    story.setup.placeholders.char_id = MAELA
    live = list(cast)
    dialog = SetupDialog(story, live)
    live[:] = [c for c in live if c.id != MAELA]  # removed on the Characters tab
    dialog._save()
    assert story.setup.placeholders.char_id is None
    dialog.deleteLater()


def test_a_save_of_setup_in_the_window_keeps_a_supporting_char(
    app, tmp_path: Path, story, cast, monkeypatch
):
    """The window hands Setup the supporting cards too: saved with nothing
    changed, {{char}} is who it was."""
    from PySide6.QtWidgets import QDialogButtonBox

    from sealedlore.gui import main_window as mw

    story.world_bible = "{{char}} keeps the only working radio."
    story.setup.placeholders.char_id = IDRIS
    bundle = StoryBundle(story=story, cast=cast[:2], nodes=make_exchange(2))
    bundle.supporting.characters.append(cast[2])
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = SERRIK
    save_story_bundle(bundle, root=tmp_path)
    window = mw.MainWindow(root=tmp_path, use_mock=True)
    try:
        window.show()
        window.open_story(story.id)
        shown: list[str] = []

        class Dialog(mw.SetupDialog):
            def exec(self):  # noqa: A003 - Qt naming
                shown.append(self.char_is.currentText())
                self.findChild(QDialogButtonBox).accepted.emit()  # Save
                return mw.SetupDialog.Accepted

        monkeypatch.setattr(mw, "SetupDialog", Dialog)
        window.open_setup()
        assert shown == ["Captain Idris"]
        assert window.session.story.setup.placeholders.char_id == IDRIS
    finally:
        window.close()
