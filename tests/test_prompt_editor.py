"""File → Prompts (advanced)…: the editor of every prompt text."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import sealedlore.gui.main_window as main_window  # noqa: E402
from sealedlore.engine.prompt import TurnRequest  # noqa: E402
from sealedlore.engine.prompt_edits import set_text  # noqa: E402
from sealedlore.engine.prompt_texts import DEFAULT_TEXTS, TEXTS  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.prompt_editor import PromptEditorDialog  # noqa: E402
from sealedlore.models.config import Config  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_config,
    save_story_bundle,
)
from tests.conftest import make_exchange  # noqa: E402

RULES = "storyteller.rules"
HELD = "turn.directives.held"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def select(dialog: PromptEditorDialog, key: str) -> None:
    item = next(item for item in dialog._items() if item.data(0, Qt.UserRole) == key)
    dialog.tree.setCurrentItem(item)


def scope(dialog: PromptEditorDialog, name: str) -> None:
    dialog.scope.setCurrentIndex(dialog.scope.findData(name))


def test_every_text_is_listed_and_shows_the_one_in_force(app):
    dialog = PromptEditorDialog(Config(), Story(title="T"))
    assert len(dialog._items()) == len(TEXTS)
    select(dialog, RULES)
    assert dialog.editor.toPlainText() == DEFAULT_TEXTS[RULES]
    assert dialog.default_view.toPlainText() == DEFAULT_TEXTS[RULES]
    dialog.deleteLater()


def test_typing_edits_this_story_only_until_saved(app):
    config, story = Config(), Story(title="T")
    dialog = PromptEditorDialog(config, story)
    select(dialog, RULES)
    dialog.editor.setPlainText("House rules.")
    assert dialog.edits["story"][RULES].text == "House rules."
    assert story.prompt_edits == {} and config.prompt_edits == {}
    dialog.apply_to(config, story)
    assert story.prompt_edits[RULES].text == "House rules." and config.prompt_edits == {}
    dialog.deleteLater()


def test_an_edit_for_every_story_shows_through_until_the_story_has_its_own(app):
    config = Config()
    set_text(config.prompt_edits, RULES, "For every story.")
    dialog = PromptEditorDialog(config, Story(title="T"))
    select(dialog, RULES)
    assert dialog.editor.toPlainText() == "For every story."
    assert "edit for every story" in dialog.status.text()
    # The default typed back for this story alone stays an edit of its own.
    dialog.editor.setPlainText(DEFAULT_TEXTS[RULES])
    assert dialog.edits["story"][RULES].text == DEFAULT_TEXTS[RULES]
    dialog.editor.setPlainText("For every story.")
    assert RULES not in dialog.edits["story"]
    dialog.deleteLater()


def test_every_story_is_the_only_scope_without_a_story(app):
    dialog = PromptEditorDialog(Config(), None)
    assert dialog.scope.count() == 1 and dialog.scope_name == "app"
    select(dialog, RULES)
    dialog.editor.setPlainText("Everywhere.")
    assert dialog.edits["app"][RULES].text == "Everywhere."
    dialog.deleteLater()


def test_a_placeholder_left_out_is_said(app):
    dialog = PromptEditorDialog(Config(), Story(title="T"))
    select(dialog, HELD)
    dialog.editor.setPlainText("Do not write them.")
    assert "{held}" in dialog.status.text() and not dialog.status.isHidden()
    dialog.deleteLater()


def test_reset_puts_the_default_back(app):
    story = Story(title="T")
    set_text(story.prompt_edits, RULES, "Mine.")
    dialog = PromptEditorDialog(Config(), story)
    select(dialog, RULES)
    assert dialog.reset_button.isEnabled()
    dialog.reset_button.click()
    assert RULES not in dialog.edits["story"]
    assert dialog.editor.toPlainText() == DEFAULT_TEXTS[RULES]
    dialog.deleteLater()


def test_search_finds_a_text_by_its_words(app):
    dialog = PromptEditorDialog(Config(), Story(title="T"))
    dialog.search.setText("welcoming party")
    shown = [item.data(0, Qt.UserRole) for item in dialog._items() if not item.isHidden()]
    assert shown == [RULES]
    dialog.deleteLater()


def test_copying_from_another_story_takes_its_edits(app, tmp_path: Path, monkeypatch):
    other = Story(title="Other")
    set_text(other.prompt_edits, RULES, "Theirs.")
    save_story_bundle(StoryBundle(story=other), root=tmp_path)
    dialog = PromptEditorDialog(Config(), Story(title="T"), root=tmp_path)
    monkeypatch.setattr(
        "sealedlore.gui.prompt_editor.QInputDialog.getItem",
        lambda *args, **kwargs: (args[3][0], True),
    )
    dialog.copy_button.click()
    assert dialog.edits["story"][RULES].text == "Theirs."
    dialog.deleteLater()


def test_saving_from_the_window_reaches_the_next_prompt(
    app, tmp_path: Path, story, cast, monkeypatch
):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)

    class Dialog(PromptEditorDialog):
        def exec(self):  # noqa: A003 - Qt naming
            scope(self, "story")
            select(self, RULES)
            self.editor.setPlainText("Story rules.")
            scope(self, "app")
            select(self, "question.rules")
            self.editor.setPlainText("Answer plainly.")
            return PromptEditorDialog.Accepted

    monkeypatch.setattr(main_window, "PromptEditorDialog", Dialog)
    assert window.prompts_action.isEnabled()
    window.prompts_action.trigger()

    assert window.session.story.prompt_edits[RULES].text == "Story rules."
    assert load_config(root=tmp_path).prompt_edits["question.rules"].text == "Answer plainly."
    turn = TurnRequest(speaker_id="char-serrik", user_text="On.")
    assert window.session.assemble(turn).messages[0].text.startswith("Story rules.")
    window.close()
