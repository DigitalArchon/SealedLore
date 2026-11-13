"""The plot editor on a story, offscreen (gui/plot_editor.py story mode,
gui/window_plot.py). The author asked for it (Nov 2026): the editor could only
start a plot from nothing, and which of its parts were the plot and which
just a story wasn't clear. Then (Nov 6 2026): anything there is editable,
but it saves as a new story, never over the story it was made from."""

from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox  # noqa: E402

from sealedlore.gui import plot_editor_pages  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.plot_editor import (  # noqa: E402
    CHARACTER,
    EVENT,
    FACT,
    FRONT,
    HEADER,
    LORE,
    OPTIONAL,
    PLACE,
    PLOT,
    STORYTELLER,
    PlotEditorWindow,
)
from sealedlore.gui.plot_editor_pages import (  # noqa: E402
    STORYTELLER_IN_A_STORY,
    CharacterPage,
    EntryPage,
    StorytellerNotesPage,
    WayPage,
)
from sealedlore.gui.plot_pictures import keep_picture  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.storage.library import story_listings  # noqa: E402
from sealedlore.storage.paths import story_dir  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
from tests.conftest import make_exchange  # noqa: E402


def _png() -> bytes:
    """A real one-pixel PNG, so the thumbnails decode."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    pixels = zlib.compress(b"\x00\x80\x40\x20\xff")
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")
    )


PNG = _png()


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    """A story not begun yet, with no plot: its cast (Serrik with a picture),
    a supporting card and two lore entries."""
    story.setup.starting_scene = story.scene.model_copy(deep=True)
    cast[0].reference_images = [ImageRef(id="ref-1", file="images/refs/ref-1.png", caption="him")]
    bundle = StoryBundle(
        story=story,
        cast=cast,
        lore=[
            LoreEntry(id="lore-keep", title="Calder Keep", content="A keep on the cliffs."),
            LoreEntry(id="lore-oath", title="The Grey Oath", content="What duellists swear."),
        ],
    )
    bundle.supporting.characters.append(Character(id="char-tobin", name="Old Tobin"))
    bundle.pending_files["images/refs/ref-1.png"] = PNG
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
    assert window.edit_plot_action.text() == "&Open in the plot editor…"
    window.edit_plot_action.trigger()
    editor = window._plot_editor
    assert editor is not None and editor.isVisible()
    return editor


def ref_of(editor: PlotEditorWindow, section: str, name: str) -> tuple:
    items = editor._items(section)
    return (section, next(i for i, item in enumerate(items) if item.name == name), -1)


def titled(monkeypatch, title: str) -> list[str]:
    """Answers Save as a new story's question with `title`, keeping what it suggested."""
    asked: list[str] = []

    def answer(_parent, _caption, _label, text="", **_kwargs):
        asked.append(text)
        return title, True

    monkeypatch.setattr(QInputDialog, "getText", answer)
    return asked


def folder_files(root: Path, story_id: str) -> dict[str, bytes]:
    folder = story_dir(story_id, root)
    return {
        p.relative_to(folder).as_posix(): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


def write_a_plot(editor: PlotEditorWindow) -> None:
    """The keep made a place, a newcomer and an event that brings them."""
    editor._select(ref_of(editor, LORE, "Calder Keep"))
    entry_page = editor.pages.currentWidget()
    assert isinstance(entry_page, EntryPage)
    entry_page.place.setChecked(True)
    editor._select((HEADER, CHARACTER, -1))
    editor._add()
    page = editor.pages.currentWidget()
    assert isinstance(page, CharacterPage)
    page.name.setText("Teodor Vask")
    page.hidden.setChecked(True)
    page.description.setPlainText("A customs captain from the capital.")
    editor._select((HEADER, FACT, -1))
    editor._add()
    editor.pages.currentWidget().name.setText("alarm")
    editor._select((HEADER, OPTIONAL, -1))
    editor._add()
    event = editor.pages.currentWidget()
    assert isinstance(event, WayPage)
    event.title.setText("The captain comes")
    event.at.setCurrentIndex(event.at.findData("Calder Keep"))
    event.tell.setPlainText("Teodor Vask rides up to the gate.")
    for i in range(event.brings.count()):
        if event.brings.item(i).data(Qt.UserRole) == "Teodor Vask":
            event.brings.item(i).setCheckState(Qt.Checked)
    editor.check_now()
    assert not [p for p in editor.problems if p.error], [p.message for p in editor.problems]


def test_the_story_comes_filled_in_and_anything_can_be_changed(app, window):
    editor = editor_on_story(window)
    assert editor.story is not None and editor.story.title == "The Sundering"
    assert not editor.story.own
    assert editor.windowTitle().startswith("The Sundering")
    assert editor.save_action.text() == "&Save as a new story…"
    assert editor.start_action.isVisible()
    label = editor.story_label.text()
    assert editor.story_label.isVisible() and "Made from your story “The Sundering”" in label
    assert "leaves “The Sundering” as it is" in label
    document = editor.document
    assert [c.name for c in document.characters] == [
        "Serrik Vaun",
        "Maela Orr",
        "Captain Idris",
        "Old Tobin",
    ]
    assert document.world.startswith("Calder Keep has stood")
    assert [entry.name for entry in document.lore] == ["Calder Keep", "The Grey Oath"]

    editor._select((HEADER, "world", -1))
    page = editor.pages.currentWidget()
    assert page.scope.text() == "The story · World"
    assert not page.world.isReadOnly() and page.banner.isHidden()

    editor._select(ref_of(editor, CHARACTER, "Serrik Vaun"))
    page = editor.pages.currentWidget()
    assert isinstance(page, CharacterPage)
    assert page.scope.text() == "The story · A character"
    assert not page.name.isReadOnly() and not page.description.isReadOnly()
    assert page.role.isEnabled() and page.hidden.isEnabled()
    assert page.pictures.isVisible() and page.pictures.list.count() == 1
    assert "(missing)" not in page.pictures.list.item(0).text()
    assert editor.remove_button.isEnabled() and editor.up_button.isEnabled()

    # Notes to oneself are a file's only; a story's background has a chooser.
    editor._select((HEADER, FRONT, -1))
    front = editor.pages.currentWidget()
    assert front.notes.isHidden() and front.background_button.isEnabled()

    editor._select((HEADER, PLOT, -1))
    assert editor.pages.currentWidget().scope.text() == "The plot · Its clock"
    editor._select((HEADER, FACT, -1))
    assert editor.section_page.scope.text() == "The plot · Facts"
    assert not editor.modified, "looking changed nothing"


def test_saving_makes_a_new_story_and_the_one_it_came_from_stays_as_it_was(
    app, window, monkeypatch, tmp_path
):
    original = window.session.story.id
    kept = folder_files(tmp_path, original)
    editor = editor_on_story(window)
    write_a_plot(editor)
    editor._select(ref_of(editor, CHARACTER, "Maela Orr"))
    editor.pages.currentWidget().description.setPlainText("She talks less now.")
    asked = titled(monkeypatch, "The Sundering, retold")
    assert editor.save()

    assert asked == ["The Sundering (copy)"]
    assert folder_files(tmp_path, original) == kept, "not a byte of it changed"
    assert window.session.story.id == original, "the main window stays where it was"
    made = next(s for s in story_listings(tmp_path) if s.title == "The Sundering, retold")
    new = load_story_bundle(made.id, root=tmp_path)
    assert new.nodes == [] and new.story.plot is not None
    vask = next(c for c in new.cast if c.name == "Teodor Vask")
    assert vask.plot_hidden and new.story.plot.events[0].brings == [vask.id]
    assert [(p.name, p.lore_id) for p in new.story.plot.places] == [("Calder Keep", "lore-keep")]
    maela = next(c for c in new.cast if c.name == "Maela Orr")
    assert maela.full_description == "She talks less now."
    serrik = next(c for c in new.cast if c.name == "Serrik Vaun")
    assert [r.file for r in serrik.reference_images] == ["images/refs/ref-1.png"]
    assert (story_dir(made.id, tmp_path) / "images/refs/ref-1.png").read_bytes() == PNG

    # The editor goes on with the new story, and saves to it from now on.
    assert editor.story.id == made.id and editor.story.own and not editor.modified
    assert editor.save_action.text() == "&Save"
    assert "made here and not begun yet" in editor.story_label.text()
    editor._select((HEADER, "world", -1))
    editor.pages.currentWidget().world.setPlainText("The keep fell in the night.")
    assert editor.save()
    assert len(asked) == 1, "no second new story"
    assert load_story_bundle(made.id, root=tmp_path).story.world_bible == (
        "The keep fell in the night."
    )
    assert len(story_listings(tmp_path)) == 2


def test_a_picture_added_in_the_editor_reaches_only_the_new_story(
    app, window, monkeypatch, tmp_path
):
    original = window.session.story.id
    kept = folder_files(tmp_path, original)
    editor = editor_on_story(window)
    folder = editor._folder()
    assert folder is not None and not folder.is_relative_to(story_dir(original, tmp_path))
    editor._select(ref_of(editor, CHARACTER, "Serrik Vaun"))
    page = editor.pages.currentWidget()
    page.pictures._pictures.append(keep_picture(PNG, ".png", folder, "Serrik Vaun", "armour"))
    page.pictures.changed.emit()
    titled(monkeypatch, "With armour")
    assert editor.save()
    assert folder_files(tmp_path, original) == kept
    made = next(s for s in story_listings(tmp_path) if s.title == "With armour")
    serrik = load_story_bundle(made.id, root=tmp_path).cast[0]
    assert [r.caption for r in serrik.reference_images] == ["him", "armour"]
    assert (story_dir(made.id, tmp_path) / serrik.reference_images[1].file).read_bytes() == PNG
    assert not folder.exists(), "the old copy of the pictures went with the old story"


def test_once_the_new_story_has_begun_saving_makes_another(app, window, monkeypatch, tmp_path):
    editor = editor_on_story(window)
    titled(monkeypatch, "First")
    assert editor.save()
    first = editor.story.id
    bundle = load_story_bundle(first, root=tmp_path)
    bundle.nodes = make_exchange(1)
    bundle.story.active_leaf_id = "a0"
    save_story_bundle(bundle, root=tmp_path)
    said = []
    monkeypatch.setattr(QMessageBox, "information", lambda _p, title, *_a, **_k: said.append(title))
    titled(monkeypatch, "Second")
    editor._select((HEADER, FACT, -1))
    editor._add()
    assert editor.save()
    assert said == ["Saved as a new story"]
    assert len(load_story_bundle(first, root=tmp_path).nodes) == 2, "the one begun is left alone"
    assert editor.story.title == "Second" and editor.story.id != first


def test_a_story_being_set_up_is_saved_to_from_its_plot(app, window, monkeypatch):
    """The plot the model wrote for a new story, opened in the editor from
    the plot's dialog: that story is the new thing, so it is saved to."""
    window.edit_story_plot(own=True)
    editor = window._plot_editor
    assert editor.story.own and editor.save_action.text() == "&Save"
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a, **_k: pytest.fail("asked"))
    editor._select((HEADER, "opening", -1))
    editor.pages.currentWidget().location.setText("The gatehouse")
    assert editor.save()
    assert window.session.story.setup.starting_scene.location == "The gatehouse"
    assert window.session.story.scene.location == "The gatehouse"
    assert window.scene_panel._scene is window.session.story.scene


def test_start_from_the_editor_saves_the_new_story_and_starts_it(app, window, monkeypatch):
    original = window.session.story.id
    started = []
    monkeypatch.setattr(MainWindow, "start_story", lambda self: started.append(self.session))
    editor = editor_on_story(window)
    titled(monkeypatch, "Ready")
    editor.start_story()
    assert window.session.story.title == "Ready" and window.session.story.id != original
    assert started == [window.session]


def test_a_copy_as_a_plot_file_takes_the_pictures_along(app, window, monkeypatch, tmp_path):
    editor = editor_on_story(window)
    target = tmp_path / "shared" / "sundering.md"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *_a, **_k: (str(target), ""))
    assert editor.save_as()
    assert "picture: images/refs/ref-1.png" in target.read_text(encoding="utf-8")
    assert (target.parent / "images" / "refs" / "ref-1.png").read_bytes() == PNG
    assert editor.story is not None and not editor.story.own, "still on the story"


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
    assert editor.story.id == "story-2"
    assert window.session.story.id == "story-1", "opening it here opens nothing there"


def test_saving_while_the_story_is_busy_says_so_and_keeps_the_work(app, window, monkeypatch):
    editor = editor_on_story(window)
    editor._select((HEADER, FACT, -1))
    editor._add()
    said = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: said.append(box.windowTitle()) or 0)
    titled(monkeypatch, "Busy")
    window._busy = True
    try:
        assert not editor.save()
    finally:
        window._busy = False
    assert said == ["The story is busy"]
    assert editor.modified and editor.document.facts
    assert len(story_listings(window.root)) == 1


def test_a_plot_file_still_opens_as_a_file(app, window):
    editor = window._plot_editor_window()
    sample = Path(__file__).resolve().parents[1] / "SampleStories" / "Doomsville.md"
    assert editor.open_path(sample)
    assert editor.story is None and not editor.story_label.isVisible()
    assert editor.save_action.text() == "&Save" and editor.start_action.isVisible()
    assert editor.start_action.text() == "Start a s&tory from this file"
    editor._select((EVENT, 0, -1))
    assert editor.pages.currentWidget().scope.text() == "The plot · An event"
    editor._select(ref_of(editor, CHARACTER, "John"))
    page = editor.pages.currentWidget()
    assert not page.name.isReadOnly() and page.banner.isHidden()
    editor._select((HEADER, FRONT, -1))
    assert not editor.pages.currentWidget().notes.isHidden()
    editor._select(ref_of(editor, PLACE, "Hellsville"))
    assert editor.pages.currentWidget().scope.text() == "The story · A place"


def test_a_background_chosen_in_the_editor_goes_behind_the_new_story(
    app, window, monkeypatch, tmp_path
):
    editor = editor_on_story(window)
    folder = editor._folder()
    chosen = keep_picture(PNG, ".png", folder, "background")
    monkeypatch.setattr(plot_editor_pages, "choose_picture", lambda *_a, **_k: chosen)
    editor._select((HEADER, FRONT, -1))
    front = editor.pages.currentWidget()
    front.background_button.click()
    assert editor.document.background == chosen.file and editor.modified
    titled(monkeypatch, "Under a new sky")
    assert editor.save()
    made = next(s for s in story_listings(tmp_path) if s.title == "Under a new sky")
    new = load_story_bundle(made.id, root=tmp_path)
    assert new.story.background_image == "images/background.png"
    assert (story_dir(made.id, tmp_path) / "images/background.png").read_bytes() == PNG
    assert window.session.story.background_image is None, "the old story has none still"


def test_a_story_s_notes_are_shown_plainly_and_saved_as_read_only_when_changed(
    app, window, monkeypatch, tmp_path
):
    """Opened in the editor, a story's notes for the storyteller are on a
    page of their own. Untouched, a new story has them as they were, unread
    if a model wrote them; changed here, whoever changed them has read them."""
    story = window.session.story
    story.storyteller_notes = "The keep's well is poisoned, and Maela did it."
    story.storyteller_notes_by = "model"
    window.session.save()
    editor = editor_on_story(window)
    editor._select((HEADER, STORYTELLER, -1))
    page = editor.pages.currentWidget()
    assert isinstance(page, StorytellerNotesPage)
    assert page.notes.toPlainText() == "The keep's well is poisoned, and Maela did it."
    assert page.readers.text() == STORYTELLER_IN_A_STORY
    assert not editor.modified

    editor._select((HEADER, "world", -1))
    editor.pages.currentWidget().world.setPlainText("The keep fell in the night.")
    titled(monkeypatch, "Untouched notes")
    assert editor.save()
    made = next(s for s in story_listings(tmp_path) if s.title == "Untouched notes")
    new = load_story_bundle(made.id, root=tmp_path).story
    assert new.storyteller_notes == "The keep's well is poisoned, and Maela did it."
    assert new.storyteller_notes_by == "model", "still unread in Setup"

    # The editor goes on with the new story: now the notes are changed.
    editor._select((HEADER, STORYTELLER, -1))
    editor.pages.currentWidget().notes.setPlainText("The cistern is poisoned, and Idris did it.")
    assert editor.save()
    new = load_story_bundle(made.id, root=tmp_path).story
    assert new.storyteller_notes == "The cistern is poisoned, and Idris did it."
    assert new.storyteller_notes_by == "author"
    # The story it all came from is as it was.
    kept = load_story_bundle(story.id, root=tmp_path).story
    assert (kept.storyteller_notes_by, kept.world_bible) == ("model", story.world_bible)
