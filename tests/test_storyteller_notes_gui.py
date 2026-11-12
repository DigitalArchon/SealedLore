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
