"""The storyteller's notes in the window: kept out of the author's sight on
the Prompt page unless they asked to see what the story hides."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.prompt import (  # noqa: E402
    SECTION_NOTES,
    AssemblyOptions,
    TurnRequest,
    assemble_prompt,
)
from sealedlore.engine.rules import notes_kept_line  # noqa: E402
from sealedlore.gui.inspector import KEPT_TIP, SECTION_LABELS, ContextInspector  # noqa: E402
from sealedlore.models.generation import GenerationParams  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402

MARKER = "ZQX-SECRET"
NOTES = f"Under the granary's false floor is the seed corn, in a crate stamped {MARKER}."
TURN = TurnRequest(
    speaker_id="char-serrik", user_text="I look around.", controlled_character_id="char-serrik"
)


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def notes_row(inspector: ContextInspector):
    for index in range(inspector.sections.topLevelItemCount()):
        item = inspector.sections.topLevelItem(index)
        if item.data(0, Qt.UserRole) == SECTION_NOTES:
            return item
    return None


def everything_shown(inspector: ContextInspector) -> str:
    """Whatever the page can show or copy: every section's text, the request
    as JSON and the prompt as text."""
    shown = [inspector.prompt_text(), inspector._payload_json]
    for index in range(inspector.sections.topLevelItemCount()):
        inspector.sections.setCurrentItem(inspector.sections.topLevelItem(index))
        shown.append(inspector.payload.toPlainText())
    return "\n".join(shown)


def show(inspector: ContextInspector, prompt, **kwargs) -> None:
    inspector.show_prompt(
        prompt,
        model="m",
        params=GenerationParams(),
        use_cache_control=True,
        approximate=True,
        **kwargs,
    )


def test_the_prompt_page_keeps_the_notes_unless_the_author_asked(app, story, cast, estimator):
    story.storyteller_notes = NOTES
    prompt = assemble_prompt(
        story=story,
        cast=cast,
        history_nodes=make_exchange(2),
        turn=TURN,
        estimator=estimator,
        options=AssemblyOptions(min_cacheable_tokens=1),
    )
    inspector = ContextInspector()

    show(inspector, prompt)
    row = notes_row(inspector)
    assert row is not None and row.text(0) == SECTION_LABELS[SECTION_NOTES] == "Storyteller's notes"
    assert KEPT_TIP in row.toolTip(0)
    shown = everything_shown(inspector)
    assert MARKER not in shown and "granary" not in shown
    # In the section, in the JSON and in the copied text: the line instead.
    assert shown.count(notes_kept_line(NOTES)) >= 3
    # The size is the real section's: the budget doesn't change with the view.
    assert row.text(1).lstrip("~") == f"{prompt.section(SECTION_NOTES).tokens:,}"

    show(inspector, prompt, secrets=True)
    row = notes_row(inspector)
    assert KEPT_TIP not in row.toolTip(0)
    shown = everything_shown(inspector)
    assert shown.count(MARKER) >= 3 and notes_kept_line(NOTES) not in shown


def test_copying_from_the_prompt_page_copies_what_it_shows(
    app, story, cast, estimator, monkeypatch
):
    story.storyteller_notes = NOTES
    prompt = assemble_prompt(
        story=story, cast=cast, history_nodes=[], turn=TURN, estimator=estimator
    )
    copied: list[str] = []
    monkeypatch.setattr("sealedlore.gui.inspector.copy_text", copied.append)
    inspector = ContextInspector()
    show(inspector, prompt)
    inspector.copy_text.click()
    inspector.copy_json.click()
    assert len(copied) == 2
    assert all(MARKER not in text and notes_kept_line(NOTES) in text for text in copied)


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    from sealedlore.gui.main_window import MainWindow

    story.storyteller_notes = NOTES
    story.storyteller_notes_by = "model"
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    yield window
    window.close()


def test_the_windows_prompt_page_follows_the_spoiler_switch(window):
    window.config.show_plot_spoilers = False
    window._reassemble_inspector()
    assert MARKER in window.session.last_prompt.section(SECTION_NOTES).text
    assert MARKER not in everything_shown(window.inspector)

    window._on_spoilers_toggled(True)
    assert MARKER in everything_shown(window.inspector)
    window._on_spoilers_toggled(False)
    assert MARKER not in everything_shown(window.inspector)
    # What is sent was never touched by what is shown.
    assert MARKER in window.session.last_prompt.section(SECTION_NOTES).text


# --- Story → Setup → Storyteller's notes ---------------------------------------------


def setup_for(story, cast=()):
    from sealedlore.gui.setup_dialog import SetupDialog

    return SetupDialog(story, list(cast))


def tab_names(dialog) -> list[str]:
    return [dialog.tabs.tabText(index) for index in range(dialog.tabs.count())]


def texts_in(dialog) -> str:
    """Every word the dialog holds where the author could read it."""
    from PySide6.QtWidgets import QLabel, QLineEdit, QPlainTextEdit

    words = [label.text() for label in dialog.findChildren(QLabel)]
    words += [box.toPlainText() for box in dialog.findChildren(QPlainTextEdit)]
    words += [line.text() for line in dialog.findChildren(QLineEdit)]
    return "\n".join(words)


def answers(monkeypatch, answer) -> list[str]:
    from PySide6.QtWidgets import QMessageBox

    titles: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda _parent, title, *_a, **_k: titles.append(title) or answer
    )
    return titles


def test_setup_has_the_tab_after_world_and_a_chat_has_none(app, story):
    from sealedlore.models.story import Story

    assert tab_names(setup_for(story))[:3] == ["About", "World", "Storyteller's notes"]
    chat = Story(title="A chat", mode="chat", storyteller_notes=NOTES)
    dialog = setup_for(chat)
    assert "Storyteller's notes" not in tab_names(dialog)
    assert MARKER not in texts_in(dialog)


def test_the_authors_own_notes_are_simply_edited(app, story):
    dialog = setup_for(story)
    assert not dialog.notes.isHidden() and dialog.notes_unread.isHidden()
    assert dialog.notes.toPlainText() == ""
    dialog.notes.setPlainText("  The ferryman is the missing heir.  ")
    dialog._save()
    assert story.storyteller_notes == "The ferryman is the missing heir."
    assert story.storyteller_notes_by == "author"

    again = setup_for(story)
    assert not again.notes.isHidden()
    assert again.notes.toPlainText() == "The ferryman is the missing heir."
    again.notes.setPlainText("")
    again._save()
    assert story.storyteller_notes is None and story.storyteller_notes_by == "author"


def test_notes_a_model_wrote_stay_unread_until_the_author_asks(app, story):
    story.storyteller_notes, story.storyteller_notes_by = NOTES, "model"
    dialog = setup_for(story)
    assert not dialog.notes_unread.isHidden() and dialog.notes.isHidden()
    words = len(NOTES.split())
    assert dialog.notes_unread_label.text() == (
        f"The storyteller has notes for this story that you have not read ({words} words)."
    )
    assert (dialog.show_notes.text(), dialog.clear_notes.text()) == ("Show them", "Clear them")
    assert MARKER not in texts_in(dialog) and "granary" not in texts_in(dialog)
    # Saved without a look, they are as they were, and nothing to ask about.
    dialog.show()
    for _ in range(3):
        app.processEvents()
    assert not dialog.edited()
    dialog.world.setPlainText("A cold country.")
    dialog._save()
    assert (story.storyteller_notes, story.storyteller_notes_by) == (NOTES, "model")
    assert story.world_bible == "A cold country."


def test_shown_and_left_alone_a_models_notes_are_unread_again_next_time(app, story):
    story.storyteller_notes, story.storyteller_notes_by = NOTES, "model"
    dialog = setup_for(story)
    dialog.show()
    for _ in range(3):
        app.processEvents()
    dialog.show_notes.click()
    assert not dialog.notes.isHidden() and dialog.notes_unread.isHidden()
    assert dialog.notes.toPlainText() == NOTES
    assert not dialog.edited(), "reading is no change: Cancel asks nothing"
    dialog._save()
    assert (story.storyteller_notes, story.storyteller_notes_by) == (NOTES, "model")
    assert setup_for(story).notes_unread_label.text().startswith("The storyteller has notes")


def test_notes_the_author_changed_are_the_authors(app, story):
    story.storyteller_notes, story.storyteller_notes_by = NOTES, "model"
    dialog = setup_for(story)
    dialog.show()
    for _ in range(3):
        app.processEvents()
    dialog.show_notes.click()
    dialog.notes.setPlainText(NOTES + "\nElena knows.")
    assert dialog.edited()
    dialog._save()
    assert story.storyteller_notes == NOTES + "\nElena knows."
    assert story.storyteller_notes_by == "author"
    again = setup_for(story)
    assert not again.notes.isHidden() and again.notes.toPlainText().endswith("Elena knows.")


def test_clearing_unread_notes_asks_first_and_is_saved_with_setup(app, story, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    story.storyteller_notes, story.storyteller_notes_by = NOTES, "model"
    dialog = setup_for(story)
    dialog.show()
    for _ in range(3):
        app.processEvents()

    asked: list[tuple] = []

    def question(_parent, title, _text, buttons, default):
        asked.append((title, buttons, default))
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "question", question)
    dialog.clear_notes.click()
    assert asked == [
        ("Clear the storyteller's notes?", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
    ]
    assert not dialog.notes_unread.isHidden() and not dialog.edited()

    titles = answers(monkeypatch, QMessageBox.Yes)
    dialog.clear_notes.click()
    assert titles == ["Clear the storyteller's notes?"]
    assert not dialog.notes.isHidden() and dialog.notes.toPlainText() == ""
    assert MARKER not in texts_in(dialog)
    # Not cleared until Save: Cancel asks about it, and leaves the story's notes.
    assert dialog.edited()
    titles = answers(monkeypatch, QMessageBox.Cancel)
    dialog.reject()
    assert titles == ["Discard your changes?"] and dialog.isVisible()
    assert story.storyteller_notes == NOTES
    dialog._save()
    assert story.storyteller_notes is None and story.storyteller_notes_by == "author"


def test_changed_notes_on_a_story_under_way_need_no_new_playthrough(app, window, monkeypatch):
    import sealedlore.gui.main_window as mw

    class Dialog(mw.SetupDialog):
        def exec(self):  # noqa: A003 - Qt naming
            self.show_notes.click()
            self.notes.setPlainText("The well is dry because Maela dammed the spring.")
            self._save()
            return mw.SetupDialog.Accepted

    monkeypatch.setattr(mw, "SetupDialog", Dialog)
    offered: list[bool] = []
    monkeypatch.setattr(window, "_offer_new_playthrough", lambda: offered.append(True))
    assert window.session.nodes, "the story has begun"
    assert window.open_setup()
    assert not offered
    story = window.session.story
    assert story.storyteller_notes == "The well is dry because Maela dammed the spring."
    assert story.storyteller_notes_by == "author"
    # The next prompt has them, and the story is saved with them.
    assert "dammed the spring" in window.session.last_prompt.section(SECTION_NOTES).text
    from sealedlore.storage.repository import load_story_bundle

    saved = load_story_bundle(story.id, root=window.root).story
    assert saved.storyteller_notes == story.storyteller_notes


# --- a draft from a premise, and asking for changes to it -------------------------------

SECRET = f"The first mate sold the Guild's charts; his ledger is marked {MARKER}."
DRAFT = {
    "title": "Salt and Tide",
    "world": "A harbour town on a cold coast, where half the town smuggles.",
    "cast": [{"name": "Mara Quill", "summary": "A boatwright with debts.", "playable": True}],
    "play_as": "Mara Quill",
    "opening": "Fog on the quays at dawn.",
    "starting_scene": {"location": "The quays", "present": ["Mara Quill"]},
    "storyteller_notes": SECRET,
}


def wait_until(app, condition, timeout: float = 20.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.005)


def test_a_draft_that_cannot_be_used_is_shown_without_its_notes(app):
    import json

    from sealedlore.gui.generate_dialog import GenerateDialog
    from sealedlore.providers.mock import MockChatProvider

    unusable = json.dumps({**DRAFT, "cast": []})
    dialog = GenerateDialog(MockChatProvider([unusable]), model="m", default_model="m")
    dialog.premise.setPlainText("A harbour town.")
    dialog._generate()
    wait_until(app, lambda: dialog._thread is None)
    assert not dialog.reply_view.isHidden(), "the reply is shown: the draft had no cast"
    shown = dialog.reply_view.toPlainText()
    assert "A harbour town on a cold coast" in shown
    assert MARKER not in shown and "sold the Guild's charts" not in shown
    assert MARKER not in texts_in(dialog)
    # Nothing typed to ask about on the way out.
    dialog._typed = dialog._as_typed()
    dialog.close()


@pytest.mark.real_follow_up
def test_after_a_draft_the_author_is_told_there_are_notes_and_none_of_them(
    app, tmp_path, monkeypatch
):
    import json

    from PySide6.QtWidgets import QDialog

    from sealedlore.gui.followup_dialog import FollowUpDialog
    from sealedlore.gui.generate_dialog import GenerateDialog
    from sealedlore.gui.main_window import MainWindow
    from sealedlore.providers.mock import MockChatProvider

    window = MainWindow(root=tmp_path, use_mock=True)
    rewritten = f"The harbour master sold them; her ledger is marked {MARKER}-TWO."
    follow_up = json.dumps(
        {
            "summary": "A stranger tide.",
            "changes": [
                {"kind": "world", "value": "A harbour where the tide keeps its own books."},
                {"kind": "storyteller_notes", "value": rewritten},
            ],
            "cannot_fix": [],
        }
    )
    provider = MockChatProvider([json.dumps(DRAFT), follow_up])
    monkeypatch.setattr(window, "_current_provider", lambda: provider)
    monkeypatch.setattr(window, "_needs_provider", lambda: False)
    monkeypatch.setattr(window, "open_setup", lambda: False)

    def draft(dialog) -> int:
        dialog.premise.setPlainText("A harbour town.")
        dialog._generate()
        wait_until(app, lambda: dialog._thread is None)
        return dialog.result()

    monkeypatch.setattr(GenerateDialog, "exec", draft)
    seen: list[str] = []

    def look(dialog: FollowUpDialog) -> int:
        seen.append(dialog.log.toPlainText())
        dialog.reveal()
        seen.append(dialog.view.toPlainText())
        dialog.request.setPlainText("Stranger, please.")
        dialog.send_button.click()
        wait_until(app, lambda: not window._busy and dialog.send_button.isEnabled())
        seen.append(dialog.log.toPlainText())
        seen.append(dialog.view.toPlainText())
        return QDialog.Accepted

    monkeypatch.setattr(FollowUpDialog, "exec", look)
    try:
        window.new_story_from_premise()
        assert len(seen) == 4
        words = len(SECRET.split())
        assert f"The draft also wrote notes for the storyteller ({words} words)" in seen[0]
        assert "Story → Setup → Storyteller's notes" in seen[0]
        assert "A harbour town on a cold coast" in seen[1], "the story, to read"
        assert "The storyteller's notes were rewritten." in seen[2]
        assert "tide keeps its own books" in seen[3]
        assert all(MARKER not in text for text in seen)
        story = window.session.story
        assert (story.storyteller_notes, story.storyteller_notes_by) == (rewritten, "model")
        # The Prompt page keeps them too, and the storyteller will have them.
        window._reassemble_inspector()
        assert MARKER not in everything_shown(window.inspector)
        assert MARKER in window.session.last_prompt.section(SECTION_NOTES).text
    finally:
        window.close()
