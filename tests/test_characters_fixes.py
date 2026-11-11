"""Bugs a review found in Your characters, the cast board's cards, the plot
editor, Setup and the new-story guide, each driven as the author meets it:
real clicks (with Ctrl and Shift) on the lists, real keys typed into the
fields."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.storage import collection  # noqa: E402
from sealedlore.storage.repository import save_story_bundle  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


# --- Your characters: the list chooses as any list does ----------------------------------


NAMES = ("Ben", "Elena", "Jane", "Marcus")


@pytest.fixture
def kept_window(app, tmp_path: Path):
    """Your characters with four of them kept, the page shown in a window."""
    root = tmp_path / "data"
    bundle = collection.load_collection(root)
    for name in NAMES:
        bundle.cast.append(collection.kept(name))
    save_story_bundle(bundle, root=root)
    window = MainWindow(root=root, use_mock=True)
    window.resize(1600, 1000)
    window.show()
    window.show_collection(True)
    settle(app)
    yield window
    window.close()


def kept_id(window: MainWindow, name: str) -> str:
    return next(c.id for c in window._kept() if c.name == name)


def click(app, page, name: str, modifier=Qt.NoModifier) -> None:
    """A real click on their row in the list, with Ctrl or Shift if given."""
    card_id = next(c.id for c in page._characters if c.name == name)
    row = next(
        row
        for row in range(page.people.count())
        if page.people.item(row).data(Qt.UserRole) == card_id
    )
    rect = page.people.visualItemRect(page.people.item(row))
    QTest.mouseClick(page.people.viewport(), Qt.LeftButton, modifier, rect.center())
    settle(app)


def chosen(page) -> list[str]:
    return sorted(page._card(card_id).name for card_id in page.chosen_ids())


def test_ctrl_click_chooses_several_as_any_list_does(app, kept_window):
    """Ctrl+click left nothing chosen, and the choice ran a click behind: the
    page re-selected the row Qt had just made current, in the middle of the
    press, so the press's own toggle undid it."""
    page = kept_window.collection_page
    click(app, page, "Elena")
    assert chosen(page) == ["Elena"]
    click(app, page, "Marcus", Qt.ControlModifier)
    assert chosen(page) == ["Elena", "Marcus"]
    assert page.title.text() == "2 characters"
    click(app, page, "Ben", Qt.ControlModifier)
    assert chosen(page) == ["Ben", "Elena", "Marcus"]
    click(app, page, "Elena", Qt.ControlModifier)
    assert chosen(page) == ["Ben", "Marcus"], "Ctrl+click on one chosen lets go of them"


def test_a_plain_click_after_several_shows_and_edits_that_one(app, kept_window):
    """Click Ben, Shift+click Jane, then click Elena: the list showed Elena
    while the title and the forms were Jane's, and what was typed went to
    Jane."""
    window = kept_window
    page = window.collection_page
    click(app, page, "Ben")
    click(app, page, "Jane", Qt.ShiftModifier)
    assert chosen(page) == ["Ben", "Elena", "Jane"]
    click(app, page, "Elena")
    assert chosen(page) == ["Elena"]
    assert page.people.currentItem().data(Qt.UserRole) == kept_id(window, "Elena")
    assert page.current == kept_id(window, "Elena")
    assert page.title.text() == "Elena"
    assert page.card_form.character is page.current_character()
    page.show_tab("Card")
    page.card_form.summary.setFocus()
    QTest.keyClicks(page.card_form.summary, "A nurse")
    window._flush_collection()
    saved = {c.name: c.summary for c in collection.load_collection(window.root).cast}
    assert saved["Elena"] == "A nurse" and not saved["Jane"]


def test_delete_after_several_asks_about_the_one_the_list_shows(app, kept_window, monkeypatch):
    """Delete acted on the card the forms still held (Jane), not the one the
    list showed (Elena)."""
    page = kept_window.collection_page
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(page, "_act", lambda what: asked.append((what, page.current)))
    click(app, page, "Ben")
    click(app, page, "Jane", Qt.ShiftModifier)
    click(app, page, "Elena")
    page.people.setFocus()
    QTest.keyClick(page.people, Qt.Key_Delete)
    assert asked == [("delete", kept_id(kept_window, "Elena"))]


def test_shift_and_arrows_choose_and_let_go_as_a_list_does(app, kept_window):
    page = kept_window.collection_page
    click(app, page, "Ben")
    page.people.setFocus()
    QTest.keyClick(page.people, Qt.Key_Down, Qt.ShiftModifier)
    settle(app)
    assert chosen(page) == ["Ben", "Elena"] and page.title.text() == "2 characters"
    QTest.keyClick(page.people, Qt.Key_Down)
    settle(app)
    assert chosen(page) == ["Jane"] and page.title.text() == "Jane"
    assert page.current == kept_id(kept_window, "Jane")


# --- Your characters: a tag for several reaches the forms of the one shown ----------------


def test_a_tag_added_to_several_is_kept_by_the_card_whose_forms_were_loaded(
    app, kept_window, monkeypatch
):
    """Elena shown, Elena and Marcus chosen, Add a tag… "winter": back on
    Elena, a key typed in her maker's notes wrote the About tab's old tags
    back over it, and "winter" was gone from the disk."""
    from PySide6.QtWidgets import QInputDialog

    window = kept_window
    page = window.collection_page
    click(app, page, "Elena")
    page.show_tab("About")
    click(app, page, "Marcus", Qt.ControlModifier)
    assert page.title.text() == "2 characters"
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("winter", True))
    page._chosen("tag")
    click(app, page, "Elena")
    assert page.title.text() == "Elena"
    assert page.about_form.tags.text() == "winter"
    page.about_form.creator_notes.setFocus()
    QTest.keyClicks(page.about_form.creator_notes, "Drawn in winter")
    window._flush_collection()
    saved = {c.name: c for c in collection.load_collection(window.root).cast}
    assert saved["Elena"].tavern.tags == ["winter"]
    assert saved["Elena"].tavern.creator_notes == "Drawn in winter"
    assert saved["Marcus"].tavern.tags == ["winter"]


def test_a_tag_taken_off_several_doesnt_come_back_on_the_card_shown(app, kept_window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    window = kept_window
    page = window.collection_page
    for name in ("Elena", "Marcus"):
        window._kept_card(kept_id(window, name)).tavern.tags = ["survival", "road"]
    window._save_collection()
    window._refresh_collection()
    click(app, page, "Elena")
    page.show_tab("About")
    assert page.about_form.tags.text() == "survival, road"
    click(app, page, "Marcus", Qt.ControlModifier)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("survival", True))
    page._chosen("untag")
    click(app, page, "Elena")
    assert page.about_form.tags.text() == "road"
    page.about_form.creator.setFocus()
    QTest.keyClicks(page.about_form.creator, "me")
    window._flush_collection()
    saved = {c.name: c for c in collection.load_collection(window.root).cast}
    assert saved["Elena"].tavern.tags == ["road"] and saved["Elena"].tavern.creator == "me"


# --- the plot editor: a name typed a key at a time renames only its own item --------------


SAMPLE = Path(__file__).resolve().parents[1] / "SampleStories" / "Doomsville.md"


@pytest.fixture
def plot_editor(app):
    from sealedlore.gui.plot_editor import PlotEditorWindow

    window = PlotEditorWindow()
    assert window.open_path(SAMPLE)
    window.show()
    yield window
    window.modified = False
    window.close()


def type_name(app, field, text: str) -> None:
    """What a person does: the field chosen whole, then the keys."""
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, text)
    settle(app)


def brought(document) -> list[list[str]]:
    return [list(way.brings) for event in document.events for way in (event, *event.variants)]


def test_a_new_characters_name_typed_through_anothers_leaves_them_be(app, plot_editor):
    """Typing "Elena Ruiz Junior" for a new character renamed Elena Ruiz, and
    every event that brings her, on the way (at "Elena Ruiz", then " ")."""
    from sealedlore.gui.plot_editor import CHARACTER, HEADER

    editor = plot_editor
    names = [c.name for c in editor.document.characters]
    brings = brought(editor.document)
    editor._select((HEADER, CHARACTER, -1))
    editor._add()
    type_name(app, editor.pages.currentWidget().name, "Elena Ruiz Junior")
    assert [c.name for c in editor.document.characters] == [*names, "Elena Ruiz Junior"]
    assert brought(editor.document) == brings
    elena = names.index("Elena Ruiz")
    assert editor._find((CHARACTER, elena, -1)).text(0) == "Elena Ruiz"
    assert editor._find((CHARACTER, len(names), -1)).text(0).startswith("Elena Ruiz Junior")
    editor.check_now()
    assert not [p for p in editor.problems if p.error]


def test_a_new_facts_name_typed_through_anothers_leaves_its_conditions_be(app, plot_editor):
    from sealedlore.gui.plot_editor import FACT, HEADER

    editor = plot_editor
    facts = [f.name for f in editor.document.facts]
    editor._select((HEADER, FACT, -1))
    editor._add()
    type_name(app, editor.pages.currentWidget().name, "hellsville_two")
    assert [f.name for f in editor.document.facts] == [*facts, "hellsville_two"]
    fall = editor.document.events[0]
    assert fall.requires[0].fact == "hellsville"
    assert fall.variants[0].sets[0].fact == "hellsville"


def test_two_new_events_one_retitled_leaves_the_other(app, plot_editor):
    from sealedlore.gui.plot_editor import HEADER, OPTIONAL

    editor = plot_editor
    editor._select((HEADER, OPTIONAL, -1))
    editor._add()
    editor._add()
    type_name(app, editor.pages.currentWidget().title, "The second one")
    titles = [e.title for e in editor.document.events]
    assert titles[-2:] == ["New event", "The second one"]


def test_a_name_emptied_and_typed_anew_keeps_what_named_it(app, plot_editor):
    """Marcus Webb backspaced to nothing, then "Helena Ruiz": the events that
    brought him brought "" (saved as nobody)."""
    from sealedlore.gui.plot_editor import CHARACTER

    editor = plot_editor
    marcus = [c.name for c in editor.document.characters].index("Marcus Webb")
    editor._select((CHARACTER, marcus, -1))
    field = editor.pages.currentWidget().name
    field.setFocus()
    field.end(False)
    for _ in "Marcus Webb":
        QTest.keyClick(field, Qt.Key_Backspace)
    assert "Marcus Webb" in [n for names in brought(editor.document) for n in names], (
        "while blank, what named him keeps his name"
    )
    QTest.keyClicks(field, "Helena Ruiz")
    settle(app)
    named = [n for names in brought(editor.document) for n in names]
    assert "Helena Ruiz" in named and "Marcus Webb" not in named and "" not in named
    editor.check_now()
    assert "- brings: Helena Ruiz, Elena Ruiz" in editor.rendered.text
    assert not [p for p in editor.problems if p.error]


def test_a_name_typed_onto_anothers_and_on_keeps_both_apart(app, plot_editor):
    """Ben renamed by typing "Marcus Webb, the elder": while his name is
    Marcus Webb's, nothing of Marcus's follows him, and his own references
    end on his new name."""
    from sealedlore.gui.plot_editor import CHARACTER

    editor = plot_editor
    names = [c.name for c in editor.document.characters]
    editor._select((CHARACTER, names.index("Ben"), -1))
    field = editor.pages.currentWidget().name
    type_name(app, field, "Marcus Webb")
    assert [c.name for c in editor.document.characters].count("Marcus Webb") == 2
    caves = next(e for e in editor.document.events if e.title == "Ben in the caves")
    assert caves.brings == ["Ben"], "on another's name, what named him keeps his own"
    editor.check_now()
    shown = [p.message for p in editor.problems]
    assert "Two characters are called “Marcus Webb”: what names them can't tell them apart." in (
        shown
    )
    QTest.keyClicks(field, " the elder")
    settle(app)
    assert caves.brings == ["Marcus Webb the elder"]
    assert ["Marcus Webb", "Elena Ruiz"] in brought(editor.document)


def test_an_ordinary_rename_still_follows_as_it_is_typed(app, plot_editor):
    from sealedlore.gui.plot_editor import CHARACTER

    editor = plot_editor
    names = [c.name for c in editor.document.characters]
    editor._select((CHARACTER, names.index("Elena Ruiz"), -1))
    type_name(app, editor.pages.currentWidget().name, "Elena Cruz")
    named = [n for names in brought(editor.document) for n in names]
    assert "Elena Cruz" in named and "Elena Ruiz" not in named
    editor.check_now()
    assert not [p for p in editor.problems if p.error]
