"""The plot editor on a story, offscreen: the story's world and cast filled
in and kept as the story has them, the plot written and saved to the story
(gui/plot_editor.py story mode, gui/window_plot.py). The author asked for it
(Nov 2026): the editor could only start a plot from nothing, and which of its
parts were the plot and which just a story wasn't clear."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.plot_editor import (  # noqa: E402
    CHARACTER,
    EVENT,
    FACT,
    HEADER,
    LORE,
    OPTIONAL,
    PLACE,
    PLOT,
    PlotEditorWindow,
)
from sealedlore.gui.plot_editor_pages import CharacterPage, EntryPage, WayPage  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
from tests.conftest import make_exchange  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    """A story not begun yet, with no plot: its cast, a supporting card and
    two lore entries."""
    story.setup.starting_scene = story.scene.model_copy(deep=True)
    bundle = StoryBundle(
        story=story,
        cast=cast,
        lore=[
            LoreEntry(id="lore-keep", title="Calder Keep", content="A keep on the cliffs."),
            LoreEntry(id="lore-oath", title="The Grey Oath", content="What duellists swear."),
        ],
    )
    bundle.supporting.characters.append(Character(id="char-tobin", name="Old Tobin"))
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1920, 1040)
    window.show()
    window.open_story(story.id)
    settle(app)
    yield window
    if window._plot_editor is not None:
        window._plot_editor.modified = False
        window._plot_editor.close()
    window.close()


def editor_on_story(window: MainWindow) -> PlotEditorWindow:
    assert window.edit_plot_action.isVisible() and window.edit_plot_action.isEnabled()
    window.edit_plot_action.trigger()
    editor = window._plot_editor
    assert editor is not None and editor.isVisible()
    return editor


def ref_of(editor: PlotEditorWindow, section: str, name: str) -> tuple:
    items = editor._items(section)
    return (section, next(i for i, item in enumerate(items) if item.name == name), -1)


def test_the_story_comes_filled_in_and_its_own_parts_stay_the_story_s(app, window):
    editor = editor_on_story(window)
    assert editor.story is not None and editor.story.title == "The Sundering"
    assert editor.windowTitle().startswith("The Sundering")
    assert "Save to the story" in editor.save_action.text()
    assert not editor.start_action.isVisible()
    assert editor.story_label.isVisible() and "The Sundering" in editor.story_label.text()
    document = editor.document
    assert [c.name for c in document.characters] == [
        "Serrik Vaun",
        "Maela Orr",
        "Captain Idris",
        "Old Tobin",
    ]
    assert document.world.startswith("Calder Keep has stood")
    assert [entry.name for entry in document.lore] == ["Calder Keep", "The Grey Oath"]

    # The story's half says where it is changed, and is read only.
    editor._select((HEADER, "world", -1))
    page = editor.pages.currentWidget()
    assert page.scope.text() == "The story · World"
    assert "Story → Setup…" in page.banner.text() and page.world.isReadOnly()

    # Someone the story begins with can't be hidden; someone it doesn't, can.
    editor._select(ref_of(editor, CHARACTER, "Serrik Vaun"))
    page = editor.pages.currentWidget()
    assert isinstance(page, CharacterPage)
    assert page.scope.text() == "The story · A character"
    assert "Cast page" in page.banner.text() and "begins with them" in page.banner.text()
    assert page.name.isReadOnly() and page.description.isReadOnly()
    assert not page.hidden.isEnabled() and not page.role.isEnabled()
    assert not page.pictures.isVisible(), "a story's pictures are on its Cast page"
    assert not editor.remove_button.isEnabled() and not editor.up_button.isEnabled()
    editor._select(ref_of(editor, CHARACTER, "Captain Idris"))
    page = editor.pages.currentWidget()
    assert page.hidden.isEnabled() and page.name.isReadOnly()

    # The plot's half is the plot's to write.
    editor._select((HEADER, PLOT, -1))
    assert editor.pages.currentWidget().scope.text() == "The plot · Its clock"
    editor._select((HEADER, FACT, -1))
    assert editor.pages.currentWidget() is editor.section_page
    assert editor.section_page.scope.text() == "The plot · Facts"
    assert not editor.modified, "looking changed nothing"


def test_a_plot_written_in_the_editor_is_saved_to_the_story(app, window, monkeypatch):
    editor = editor_on_story(window)
    session = window.session
    serrik_before = session.cast[0].model_dump()

    # The keep becomes one of the plot's places.
    editor._select(ref_of(editor, LORE, "Calder Keep"))
    entry_page = editor.pages.currentWidget()
    assert isinstance(entry_page, EntryPage) and entry_page.place.isEnabled()
    entry_page.place.setChecked(True)
    assert [p.name for p in editor.document.places] == ["Calder Keep"]
    assert editor.current_ref() == (PLACE, 0, -1)

    # A newcomer, hidden until the event brings them in.
    editor._select((HEADER, CHARACTER, -1))
    editor._add()
    page = editor.pages.currentWidget()
    assert isinstance(page, CharacterPage) and page.hidden.isChecked()
    assert not page.name.isReadOnly() and "New:" in page.banner.text()
    assert not page.present.isEnabled(), "who the story begins with is its own"
    page.name.setText("Teodor Vask")
    page.description.setPlainText("A customs captain from the capital.")

    editor._select((HEADER, FACT, -1))
    editor._add()
    editor.pages.currentWidget().name.setText("alarm")
    editor._select((HEADER, OPTIONAL, -1))
    editor._add()
    event = editor.pages.currentWidget()
    assert isinstance(event, WayPage)
    assert event.scope.text() == "The plot · An event"
    event.title.setText("The captain comes")
    event.at.setCurrentIndex(event.at.findData("Calder Keep"))
    event.tell.setPlainText("Teodor Vask rides up to the gate.")
    for i in range(event.brings.count()):
        if event.brings.item(i).data(Qt.UserRole) == "Teodor Vask":
            event.brings.item(i).setCheckState(Qt.Checked)
    editor.check_now()
    assert not [p for p in editor.problems if p.error], [p.message for p in editor.problems]
    assert editor.modified

    assert editor.save()
    plot = session.story.plot
    vask = next(c for c in session.cast if c.name == "Teodor Vask")
    assert vask.plot_hidden
    assert [(p.name, p.lore_id) for p in plot.places] == [("Calder Keep", "lore-keep")]
    assert plot.events[0].brings == [vask.id] and plot.events[0].place == "Calder Keep"
    assert plot.facts[0].name == "alarm"
    assert session.cast[0].model_dump() == serrik_before
    assert load_story_bundle(session.story.id, root=window.root).story.plot is not None
    # Read back: the newcomer is the story's card now, still the plot's to change.
    assert not editor.modified
    vask_doc = next(c for c in editor.document.characters if c.name == "Teodor Vask")
    assert vask_doc.story_id == vask.id
    # The Plot page shows, for a story that has a plot now.
    assert window.right_tabs.isTabVisible(window.right_tabs.indexOf(window.plot_panel))


def test_a_story_under_way_keeps_whoever_it_has_shown(app, window, tmp_path):
    session = window.session
    session.bundle.nodes = make_exchange(1)
    session.story.active_leaf_id = "a0"
    session.save()
    editor = editor_on_story(window)
    assert editor.story.begun
    editor._select(ref_of(editor, CHARACTER, "Captain Idris"))
    page = editor.pages.currentWidget()
    assert not page.hidden.isEnabled()
    assert "has begun" in page.banner.text()
    editor._select(ref_of(editor, LORE, "The Grey Oath"))
    page = editor.pages.currentWidget()
    assert not page.hidden.isEnabled() and page.place.isEnabled()


def test_file_open_a_story_lists_the_author_s_stories(app, window, monkeypatch, tmp_path):
    other = StoryBundle(
        story=window.session.story.model_copy(update={"id": "story-2", "title": "Other"})
    )
    save_story_bundle(other, root=tmp_path)
    editor = window._plot_editor_window()
    assert editor.open_story_action.isVisible()
    asked = {}

    def choose(parent, title, label, items, current, editable):
        asked["items"] = items
        return next(item for item in items if item.startswith("Other")), True

    monkeypatch.setattr(QInputDialog, "getItem", choose)
    editor.open_story_chosen()
    assert any(item.startswith("The Sundering — no plot yet") for item in asked["items"])
    assert editor.story.id == "story-2" and window.session.story.id == "story-2"


def test_saving_while_the_story_is_busy_says_so_and_keeps_the_work(app, window, monkeypatch):
    editor = editor_on_story(window)
    editor._select((HEADER, FACT, -1))
    editor._add()
    said = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: said.append(box.windowTitle()) or 0)
    window._busy = True
    try:
        assert not editor.save()
    finally:
        window._busy = False
    assert said == ["The story is busy"]
    assert editor.modified and editor.document.facts
    assert window.session.story.plot is None


def test_a_plot_file_still_opens_as_a_file(app, window):
    editor = window._plot_editor_window()
    sample = Path(__file__).resolve().parents[1] / "SampleStories" / "Doomsville.md"
    assert editor.open_path(sample)
    assert editor.story is None and not editor.story_label.isVisible()
    assert editor.save_action.text() == "&Save" and editor.start_action.isVisible()
    editor._select((EVENT, 0, -1))
    assert editor.pages.currentWidget().scope.text() == "The plot · An event"
    editor._select(ref_of(editor, CHARACTER, "John"))
    page = editor.pages.currentWidget()
    assert not page.name.isReadOnly() and page.banner.isHidden()
