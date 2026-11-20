"""Lorebooks in the window (gui/lorebooks_page.py, gui/window_lorebooks.py):
kept, changed, exported, deleted, and a story started from one with the
people it describes made the story's characters; and the Lore page's
Find the people…, From Lorebooks… and Keep in Lorebooks…."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from sealedlore.engine.lore_people import LorePeople
from sealedlore.gui import lore_people_dialog, lorebooks_page, start_page, window_lorebooks
from sealedlore.gui.lorebook_story import LorebookStory
from sealedlore.gui.main_window import MainWindow
from sealedlore.storage.lorebooks import load_lorebooks, lorebook_path
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.tavern_lore import book_from_lore
from tests.test_lore_people import book, found_in


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def window_on(tmp_path: Path, story=None, cast=()) -> MainWindow:
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    if story is not None:
        save_story_bundle(StoryBundle(story=story, cast=list(cast)), root=tmp_path / "data")
        window.open_story(story.id)
    return window


def book_file(tmp_path: Path) -> Path:
    path = tmp_path / "doomsville.json"
    path.write_text(json.dumps(book()))
    return path


def import_one(window: MainWindow, tmp_path: Path, monkeypatch):
    path = book_file(tmp_path)
    monkeypatch.setattr(
        "sealedlore.gui.file_dialogs.open_file_names", lambda *a, **k: ([str(path)], "")
    )
    window.lorebooks_page.action_requested.emit(lorebooks_page.IMPORT, "")
    (stored,) = window._lorebooks()
    return stored


def test_the_start_page_and_the_file_menu_open_lorebooks(app, tmp_path: Path):
    window = window_on(tmp_path)
    assert start_page.LOREBOOKS in [kind for kind, _title, _text in start_page.OWN]
    assert window.lorebooks_action.text() == "Lorebooks…"
    window._start_choice(start_page.LOREBOOKS, None)
    assert window.lorebooks_shown() and window._page_covers()
    page = window.lorebooks_page
    assert page.pages.currentWidget() is page.empty
    assert page.back_button.text() == "Close"
    window.show_lorebooks(False)
    assert not window.lorebooks_shown()
    window.close()


def test_a_lorebook_is_imported_changed_and_saved(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    stored = import_one(window, tmp_path, monkeypatch)
    page = window.lorebooks_page
    assert window.lorebooks_shown() and page.current() == stored.id
    assert "6 entries" in page.stats.text() and "doomsville.json" in page.stats.text()
    assert page.form.list.count() == 6
    page.name.setText("Doomsville lore")
    page.name.editingFinished.emit()
    page.form.list.setCurrentRow(1)
    page.form.content.setPlainText("John Hale, written again.")
    window._flush_lorebooks()
    (saved,) = load_lorebooks(tmp_path / "data").books
    assert saved.name == "Doomsville lore"
    assert saved.book["entries"]["1"]["content"] == "John Hale, written again."
    window.close()


def test_exported_duplicated_and_deleted(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    stored = import_one(window, tmp_path, monkeypatch)
    out = tmp_path / "out.json"
    monkeypatch.setattr(
        "sealedlore.gui.file_dialogs.save_file_name", lambda *a, **k: (str(out), "")
    )
    window.lorebooks_page.action_requested.emit(lorebooks_page.EXPORT, stored.id)
    assert json.loads(out.read_text()) == stored.book
    window.lorebooks_page.action_requested.emit(lorebooks_page.DUPLICATE, stored.id)
    names = sorted(b.name for b in load_lorebooks(tmp_path / "data").books)
    assert names == ["doomsville", "doomsville (copy)"]
    trashed: list[str] = []

    def trash(path: Path) -> bool:
        trashed.append(json.loads(Path(path).read_text())["entries"]["1"]["comment"])
        return True

    monkeypatch.setattr("sealedlore.gui.window_collection.move_to_trash", trash)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.lorebooks_page.action_requested.emit(lorebooks_page.DELETE, stored.id)
    assert trashed == ["[👤] John Hale"]  # a SillyTavern lorebook in the Trash
    assert not lorebook_path(stored.id, tmp_path / "data").exists()
    assert [b.name for b in window._lorebooks()] == ["doomsville (copy)"]
    window.close()


def answer_story(monkeypatch, chosen: LorebookStory) -> None:
    monkeypatch.setattr(
        "sealedlore.gui.window_lorebooks.LorebookStoryDialog.exec", lambda self: QDialog.Accepted
    )
    monkeypatch.setattr(
        "sealedlore.gui.window_lorebooks.LorebookStoryDialog.chosen", lambda self: chosen
    )


def test_a_story_from_a_lorebook_has_its_lore_and_whom_the_author_plays(
    app, tmp_path: Path, monkeypatch
):
    window = window_on(tmp_path)
    stored = import_one(window, tmp_path, monkeypatch)
    answer_story(monkeypatch, LorebookStory("Dust", "Jane Hale", "", False, False))
    setups: list[str] = []
    monkeypatch.setattr(window, "open_setup", lambda: setups.append("setup") or True)
    monkeypatch.setattr(window, "start_story", lambda: setups.append("start"))
    window.lorebooks_page.action_requested.emit(lorebooks_page.STORY, stored.id)
    session = window.session
    assert session is not None and session.story.title == "Dust"
    assert setups == ["setup", "start"] and not window.lorebooks_shown()
    assert len(session.bundle.lore) == 6 and session.cast[0].name == "Jane Hale"
    # The story's lore is its own copy: the kept one is as it was.
    session.bundle.lore[0].content = "changed"
    assert window._kept_lorebook(stored.id).book == book()
    window.close()


def answer_people(monkeypatch, places: dict[str, str], keep: set[str], rules: bool) -> None:
    def exec_(dialog) -> int:
        for person, combo, box in zip(
            dialog._found.people, dialog._places or [None] * 99, dialog._keeps, strict=False
        ):
            if combo is not None and person.name in places:
                combo.setCurrentIndex(combo.findData(places[person.name]))
            box.setChecked(person.name in keep)
        for box in dialog._rules:
            box.setChecked(rules)
        return QDialog.Accepted

    monkeypatch.setattr(lore_people_dialog.LorePeopleDialog, "exec", exec_)


def test_a_story_from_a_lorebook_makes_its_people_cards(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    stored = import_one(window, tmp_path, monkeypatch)
    answer_story(monkeypatch, LorebookStory("Dust", "Jane Hale", "", False, True))
    monkeypatch.setattr(window, "open_setup", lambda: True)
    monkeypatch.setattr(window, "start_story", lambda: None)
    monkeypatch.setattr(
        window, "_find_people", lambda session, entries, world: found_in(list(entries))
    )
    answer_people(monkeypatch, {"John Hale": "cast"}, keep={"John Hale"}, rules=True)
    window.lorebooks_page.action_requested.emit(lorebooks_page.STORY, stored.id)
    session = window.session
    assert [c.name for c in session.cast] == ["Jane Hale", "John Hale"]
    assert [c.name for c in session.supporting] == ["Elena Ruiz", "Marcus Webb"]
    titles = [e.title for e in session.bundle.lore]
    assert titles == ["Doomsville", "Hellsville", "The Farmhouse"]
    assert "Never resolve a threat" in session.story.style.notes
    saved = load_story_bundle(session.story.id, root=tmp_path / "data")
    assert [c.name for c in saved.supporting.characters] == ["Elena Ruiz", "Marcus Webb"]
    # Kept in Your characters too, and linked to the story's card.
    assert [c.name for c in window._kept()] == ["John Hale"]
    assert session.cast[1].linked is not None
    window.close()


def test_found_in_a_kept_lorebook_they_are_kept_in_your_characters(
    app, tmp_path: Path, monkeypatch
):
    window = window_on(tmp_path)
    stored = import_one(window, tmp_path, monkeypatch)
    monkeypatch.setattr(
        window, "_find_people", lambda session, entries, world: found_in(list(entries))
    )
    answer_people(monkeypatch, {}, keep={"John Hale", "Elena Ruiz"}, rules=False)
    window.lorebooks_page.action_requested.emit(lorebooks_page.PEOPLE, stored.id)
    kept = {c.name: c for c in window._kept()}
    assert set(kept) == {"John Hale", "Elena Ruiz"}
    assert kept["John Hale"].full_description.startswith("[General Info]")
    assert kept["Elena Ruiz"].tavern.tags == ["doomsville"]
    assert kept["Elena Ruiz"].collection.came_from == "the lorebook doomsville"
    # The lorebook itself is as it was.
    assert window._kept_lorebook(stored.id).book == book()
    window.close()


def test_the_lore_page_finds_people_and_trades_with_lorebooks(
    app, tmp_path: Path, monkeypatch, story, cast
):
    from tests.test_lore_people import lore

    story.held_character_id = None
    window = window_on(tmp_path, story, cast)
    session = window.session
    session.add_lore(lore())
    monkeypatch.setattr(
        window, "_find_people", lambda session, entries, world: found_in(list(entries))
    )
    answer_people(monkeypatch, {"Elena Ruiz": "lore", "Marcus Webb": "lore"}, set(), False)
    window.lore_panel.people_requested.emit()
    assert "John Hale" in [c.name for c in session.supporting]
    assert "Tone" in [e.title for e in session.bundle.lore]  # left as lore
    # The story's lore kept as a lorebook, and brought back from one.
    window.lore_panel.to_lorebooks_requested.emit()
    (kept,) = window._lorebooks()
    assert kept.name == "The Sundering lore" and kept.came_from == "the story The Sundering"
    count = len(session.bundle.lore)
    monkeypatch.setattr(window, "_choose_their_lore", lambda groups: [groups[0][1][:2]])
    window.lore_panel.from_lorebooks_requested.emit()
    assert len(session.bundle.lore) == count + 2
    window.close()


def test_a_story_that_already_has_them_takes_their_entries_out_with_no_card(
    app, tmp_path: Path, monkeypatch, story, cast
):
    from sealedlore.models.character import Character
    from tests.test_lore_people import lore

    cast = [*cast, Character(name="John Hale")]
    window = window_on(tmp_path, story, cast)
    window.session.add_lore(lore())
    found: list[LorePeople] = []

    def find(session, entries, world):
        found.append(found_in(list(entries)))
        return found[-1]

    monkeypatch.setattr(window, "_find_people", find)
    offered: list[str] = []

    def exec_(dialog) -> int:
        offered.extend(combo.currentData() for combo in dialog._places)
        return QDialog.Accepted

    monkeypatch.setattr(lore_people_dialog.LorePeopleDialog, "exec", exec_)
    window.lore_panel.people_requested.emit()
    assert offered == ["drop", "supporting", "supporting"]
    assert [c.name for c in window.session.cast].count("John Hale") == 1
    assert "John Hale" not in [e.title for e in window.session.bundle.lore]
    window.close()


def test_a_cards_own_lorebook_is_kept_in_lorebooks(app, tmp_path: Path, monkeypatch):
    from sealedlore.storage import collection
    from tests.test_lore_people import lore

    window = window_on(tmp_path)
    card = collection.kept("Elena Ruiz")
    card.tavern.character_book = book_from_lore(lore())
    window._collection_session().cast.append(card)
    window._save_collection()
    window._kept_book_to_lorebooks(card.id)
    (kept,) = window._lorebooks()
    assert kept.name == "Elena Ruiz's lorebook" and window.lorebooks_shown()
    assert kept.book == card.tavern.character_book and kept.book is not card.tavern.character_book
    window.close()


def test_the_arrival_question_offers_the_story_only_when_one_can_take_it(
    app, tmp_path: Path, monkeypatch, story, cast
):
    window = window_on(tmp_path, story, cast)
    seen: list[list[str]] = []

    def exec_(box) -> int:
        seen.append([button.text() for button in box.buttons()])
        return QMessageBox.Cancel

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    from sealedlore.storage.tavern_lore import ReadLorebook

    assert window._ask_lorebook_arrival(ReadLorebook(book()), "d", True) == ""
    assert "Add to this story" in seen[0] and "Keep in Lorebooks" in seen[0]
    assert window._ask_lorebook_arrival(ReadLorebook(book()), "d", False) == ""
    assert "Add to this story" not in seen[1]
    assert window_lorebooks.KEEP == "keep"
    window.close()
