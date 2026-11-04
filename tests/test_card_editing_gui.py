"""Your characters, in the real window: every part of a character's card can be
changed, added to or taken off (the lorebook entry by entry, the card's
files, the fields for other apps), a copy changed instead of the original,
and the card exported as changed (the author, Nov 2026)."""

# ruff: noqa: F811 - the window's fixtures come from test_card_files_gui

from __future__ import annotations

import io
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QInputDialog, QMessageBox  # noqa: E402

from sealedlore.gui import window_collection  # noqa: E402
from sealedlore.storage import collection  # noqa: E402
from sealedlore.storage import tavern_lore as lore  # noqa: E402
from sealedlore.storage.paths import COLLECTION_ID  # noqa: E402
from sealedlore.storage.picture_store import DiskPictures  # noqa: E402
from sealedlore.storage.tavern_cards import tavern_from_file  # noqa: E402
from tests.test_card_files import png_of  # noqa: E402
from tests.test_card_files_gui import app, no_boxes, window  # noqa: E402, F401


def shown(window):
    elena = window._kept()[0]
    window.show_collection(True, elena.id)
    return elena, window.collection_page


def on_disk(window):
    window._flush_collection()
    return collection.load_collection(window.root).cast[0]


@pytest.fixture
def yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)


def test_the_lorebook_tab_adds_changes_and_removes_entries(app, window, yes):
    elena, page = shown(window)
    form = page.lorebook_form
    before = [dict(raw) for raw in lore.book_entries(elena.tavern.character_book)]
    assert [lore.entry_title(raw) or raw["keys"][0] for raw in before] == ["Hellsville"]
    form.add_button.click()
    form.title.setText("The creek")
    form.keys.setText("creek, water")
    form.content.setPlainText("Cold all year.")
    form.constant.setChecked(True)
    form.secondary.setText("winter")
    raw = lore.book_entries(elena.tavern.character_book)[-1]
    assert (raw["comment"], raw["keys"], raw["content"], raw["constant"]) == (
        "The creek",
        ["creek", "water"],
        "Cold all year.",
        True,
    )
    assert raw["secondary_keys"] == ["winter"]
    assert form.list.item(1).text() == "The creek · always sent"
    # The rest of the entry, as JSON: applied while it reads as an object.
    form.entry_rest.button.click()
    form.entry_rest.text.setPlainText('{"insertion_order": 5, "extensions": {"depth": 2}}')
    assert raw["insertion_order"] == 5 and raw["extensions"] == {"depth": 2}
    form.entry_rest.text.setPlainText('{"insertion_order": ')
    assert raw["insertion_order"] == 5 and not form.entry_rest.problem.isHidden()
    form.book_name.setText("Elena's lore")
    form.copy_button.click()
    assert form.list.count() == 3
    form.remove_button.click()
    assert form.list.count() == 2
    kept = on_disk(window)
    book = kept.tavern.character_book
    assert book["name"] == "Elena's lore"
    assert [e["content"] for e in lore.book_entries(book)] == [
        before[0]["content"],
        "Cold all year.",
    ]
    assert lore.book_entries(book)[0] == before[0], "the entry already there untouched"
    assert lore.book_entries(book)[1]["extensions"] == {"depth": 2}


def test_the_files_tab_adds_renames_retypes_links_and_removes(
    app, window, tmp_path, monkeypatch, yes
):
    elena, page = shown(window)
    form = page.files_form
    song, picture = tmp_path / "theme.mp3", tmp_path / "dusk.png"
    song.write_bytes(b"ID3 the theme")
    picture.write_bytes(png_of(90))
    monkeypatch.setattr(
        window_collection.QFileDialog,
        "getOpenFileNames",
        lambda *a, **k: ([str(song), str(picture)], ""),
    )
    form.add_button.click()
    added = elena.tavern.assets[-2:]
    folder = f"{collection.FILES_DIR}/{elena.id}/"
    assert [(a.name, a.ext, a.file) for a in added] == [
        ("theme", "mp3", f"{folder}theme.mp3"),
        ("dusk", "png", f"{folder}dusk.png"),
    ]
    store = DiskPictures(COLLECTION_ID, window.root)
    assert store.read(added[0].file) == b"ID3 the theme"
    assert form.chosen_index() == len(elena.tavern.assets) - 1, "the last one added is chosen"
    # Renamed: the file follows the name; retyped.
    form.name.setText("Dusk sky")
    form.name.editingFinished.emit()
    assert added[1].file == f"{folder}Dusk sky.png" and store.read(f"{folder}dusk.png") is None
    form.type.setCurrentText("background")
    assert added[1].type == "background"
    # A link: https only, by the HTTPS rule, though it is never fetched.
    said = no_boxes(monkeypatch)
    answers = iter([("http://example.com/x.png", True), ("https://example.com/x.png", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: next(answers))
    form.link_button.click()
    assert "https" in said[-1] and not any(a.uri.startswith("http:") for a in elena.tavern.assets)
    form.link_button.click()
    assert elena.tavern.assets[-1].uri == "https://example.com/x.png"
    # Taken off: its file goes too.
    index = elena.tavern.assets.index(added[0])
    form.files.setCurrentRow(index)
    form.files.item(index).setSelected(True)
    form.remove_button.click()
    assert added[0] not in elena.tavern.assets and store.read(added[0].file) is None
    kept = on_disk(window)
    assert any(a.name == "Dusk sky" and a.type == "background" for a in kept.tavern.assets)


def test_the_rest_of_the_card_can_be_changed_too(app, window):
    elena, page = shown(window)
    about, chats = page.about_form, page.chat_form
    about.source.setPlainText("https://example.com/elena\nchub:elena")
    about.extensions.text.setPlainText('{"talkativeness": "0.8"}')
    about.notes_elsewhere.text.setPlainText('{"de": "Eine langsame Geschichte."}')
    chats.group_greetings.list.setCurrentRow(-1)
    chats.group_greetings._add()
    chats.group_greetings.text.setPlainText("Everyone, inside. Now.")
    kept = on_disk(window)
    assert kept.tavern.source == ["https://example.com/elena", "chub:elena"]
    assert kept.tavern.extensions == {"talkativeness": "0.8"}
    assert kept.tavern.creator_notes_multilingual == {"de": "Eine langsame Geschichte."}
    assert kept.tavern.group_only_greetings == ["Everyone, inside. Now."]


def test_a_copy_is_changed_and_the_original_kept(app, window):
    elena, page = shown(window)
    window._duplicate_kept(elena.id)
    copy = next(c for c in window._kept() if c is not elena)
    window.show_collection(True, copy.id)
    entries = len(lore.book_entries(elena.tavern.character_book))
    page.lorebook_form.add_button.click()
    page.lorebook_form.content.setPlainText("Only the copy knows this.")
    page.card_form.name.setText("Elena, older")
    assert len(lore.book_entries(copy.tavern.character_book)) == entries + 1
    assert len(lore.book_entries(elena.tavern.character_book)) == entries
    assert elena.name == "Elena"
    mine = {a.file for a in elena.tavern.assets if a.held}
    theirs = {a.file for a in copy.tavern.assets if a.held}
    assert mine and theirs and not mine & theirs, "files of their own"


def test_an_export_carries_every_change(app, window, yes):
    elena, page = shown(window)
    page.lorebook_form.add_button.click()
    page.lorebook_form.title.setText("Curfew")
    page.lorebook_form.content.setPlainText("Nobody goes out after dark.")
    page.about_form.source.setPlainText("https://example.com/elena")
    files = page.files_form
    farm = next(i for i, a in enumerate(elena.tavern.assets) if a.name == "Farmhouse")
    files.files.setCurrentRow(farm)
    files.files.item(farm).setSelected(True)
    files.name.setText("The farmhouse")
    files.name.editingFinished.emit()
    card = tavern_from_file(io.BytesIO(window._charx_of(elena)), "elena.charx")
    assert [
        (e.title, e.content) for e in lore.lore_from_book(card.fields["character_book"]).entries
    ][-1] == ("Curfew", "Nobody goes out after dark.")
    assert card.fields["source"] == ["https://example.com/elena"]
    assert any(f.name == "The farmhouse" for f in card.files)
    assert Path(window.root).exists()
