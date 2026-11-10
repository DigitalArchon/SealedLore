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

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QInputDialog, QMessageBox  # noqa: E402

from sealedlore.gui import window_collection  # noqa: E402
from sealedlore.gui.lorebook_form import LorebookForm  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.tavern import TavernFields  # noqa: E402
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
    form.order.setValue(5)
    assert raw["insertion_order"] == 5
    form.entry_rest.button.click()
    form.entry_rest.text.setPlainText('{"extensions": {"depth": 2}}')
    assert raw["extensions"] == {"depth": 2}
    form.entry_rest.text.setPlainText('{"extensions": ')
    assert raw["extensions"] == {"depth": 2} and not form.entry_rest.problem.isHidden()
    assert "not applied" in form.entry_rest.button.text()
    # A long book is found by its words.
    form.find.setText("cold")
    hidden = [form.list.isRowHidden(row) for row in range(form.list.count())]
    assert hidden == [True, False]
    form.find.clear()
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


def book_form(book):
    """The Lorebook tab on its own, showing a character with `book`."""
    form = LorebookForm()
    form.load(Character(name="Elena", tavern=TavernFields(character_book=book)))
    return form


def test_a_pattern_typed_with_a_comma_stays_one_keyword(app):
    book = {"entries": [{"id": 0, "keys": [r"/\b\d{1,3} men\b/i", "farm"], "content": "c"}]}
    form = book_form(book)
    form.keys.setText(form.keys.text() + ", barn")
    assert book["entries"][0]["keys"] == [r"/\b\d{1,3} men\b/i", "farm", "barn"]
    form.secondary.setText("/gate,door/i, wall")
    assert book["entries"][0]["secondary_keys"] == ["/gate,door/i", "wall"]


def test_the_case_tick_and_the_json_say_the_same(app):
    """Ticked, a card's entry's extensions change; the JSON box showing the
    old ones put the old value back with its next edit."""
    entry = {
        "id": 0,
        "keys": ["US"],
        "content": "c",
        "enabled": True,
        "extensions": {"case_sensitive": None, "probability": 100},
    }
    book = {"entries": [entry]}
    form = book_form(book)
    form.case_sensitive.setChecked(True)
    assert entry["extensions"]["case_sensitive"] is True
    text = form.entry_rest.text.toPlainText()
    assert '"case_sensitive": true' in text
    form.entry_rest.text.setPlainText(text.replace('"probability": 100', '"probability": 50'))
    assert entry["extensions"] == {"case_sensitive": True, "probability": 50}
    assert lore.world_from_book(book)["entries"]["0"]["caseSensitive"] is True
    # Changed in the JSON, the tick follows what the extensions now say.
    form.case_sensitive.setChecked(False)
    assert entry["case_sensitive"] is False and entry["extensions"]["case_sensitive"] is False
    form.entry_rest.text.setPlainText(text)
    assert entry["extensions"]["case_sensitive"] is True and form.case_sensitive.isChecked()


def test_how_second_keywords_count_is_chosen_as_sillytavern_reads_it(app):
    """One line said "sent only when one of these is mentioned too", wrong for
    three of SillyTavern's four ways; the way is chosen now, where it reads it."""
    entry = {"id": 0, "keys": ["Hellsville"], "content": "c", "enabled": True}
    entry.update({"secondary_keys": ["the walls"], "selective": True, "extensions": {}})
    book = {"entries": [entry]}
    form = book_form(book)
    assert form.logic.isEnabled() and form.logic.currentData() == 0
    form.logic.setCurrentIndex(form.logic.findData(2))  # not sent if one of them is mentioned
    assert entry["extensions"]["selectiveLogic"] == 2
    assert '"selectiveLogic": 2' in form.entry_rest.text.toPlainText()
    assert lore.world_from_book(book)["entries"]["0"]["selectiveLogic"] == 2
    form.selective.setChecked(False)
    assert not form.logic.isEnabled() and entry["selective"] is False
    numbered = {"entries": {"0": {"uid": 0, "key": ["a"], "content": "c", "selectiveLogic": 3}}}
    form = book_form(numbered)
    assert form.logic.currentData() == 3
    form.logic.setCurrentIndex(form.logic.findData(1))
    assert numbered["entries"]["0"]["selectiveLogic"] == 1


def test_a_new_entry_is_the_one_chosen_and_shown(app):
    book = {
        "entries": {
            "0": {"uid": 0, "key": ["farm"], "content": "The farm.", "displayIndex": 5},
            "1": {"uid": 1, "key": ["creek"], "content": "The creek.", "displayIndex": 6},
        }
    }
    form = book_form(book)
    form.add_button.click()
    form.content.setPlainText("Red and empty.")
    assert [e["content"] for e in lore.book_entries(book)] == [
        "The farm.",
        "The creek.",
        "Red and empty.",
    ], "typed into the new entry, not the creek"
    # A search left in the box doesn't hide the next one, nor a copy.
    form.find.setText("farm")
    form.copy_button.click()
    assert form.find.text() == "" and not form.list.isRowHidden(form.list.currentRow())
    assert form._current()["content"] == "Red and empty." and form._current()["uid"] == 3
    form.find.setText("creek")
    form.add_button.click()
    assert not form.list.isRowHidden(form.list.currentRow())
    assert form._current()["uid"] == 4 and form._current()["content"] == ""


def test_sillytaverns_own_entry_is_edited_under_its_own_names(app):
    raw = {"uid": 3, "content": "Cold.", "disable": True}
    form = book_form({"entries": {"3": raw}})
    form.keys.setText("creek, water")
    form.secondary.setText("winter")
    form.order.setValue(40)
    form.case_sensitive.setChecked(True)
    form.enabled.setChecked(True)
    assert (raw["key"], raw["keysecondary"], raw["order"], raw["caseSensitive"]) == (
        ["creek", "water"],
        ["winter"],
        40,
        True,
    )
    assert raw["disable"] is False
    assert not {"keys", "secondary_keys", "insertion_order", "case_sensitive", "enabled"} & set(raw)


def test_an_emptied_json_box_takes_nothing_away(app):
    entry = {"uid": 3, "key": ["creek"], "content": "Cold.", "position": 4, "probability": 50}
    form = book_form({"entries": {"3": entry}})
    form.entry_rest.button.click()
    form.entry_rest.text.setPlainText("")
    assert entry["uid"] == 3 and entry["position"] == 4 and entry["probability"] == 50
    assert form.entry_rest.broken() and "{}" in form.entry_rest.problem.text()
    form.entry_rest.text.setPlainText("{}")
    assert entry == {"key": ["creek"], "content": "Cold."}, "written out, it clears"


def test_an_order_that_isnt_a_whole_number_shows_as_one(app):
    book = {
        "entries": [
            {"id": 0, "keys": ["a"], "content": "a", "insertion_order": 7.5},
            {"id": 1, "keys": ["b"], "content": "b", "insertion_order": "40"},
            {"id": 2, "keys": ["c"], "content": "c", "insertion_order": "soon"},
        ]
    }
    form = book_form(book)
    shown = []
    for row in range(3):
        form.list.setCurrentRow(row)
        shown.append(form.order.value())
    assert shown == [7, 40, 100]
    assert book["entries"][0]["insertion_order"] == 7.5, "shown, not written"


def shown_names(form) -> list[str]:
    return [form.list.item(row).text() for row in range(form.list.count())]


def test_entries_move_up_and_down_and_stay_chosen(app):
    """Move up / Move down, and Alt+Up / Alt+Down in the list, for the chosen
    entry, which stays chosen. A card's book renumbers the display index
    SillyTavern wrote on its entries, which the JSON box shows."""
    from PySide6.QtTest import QTest

    from sealedlore.gui.lorebook_form import MOVE_UP_TIP, SEARCH_HIDES

    book = {
        "entries": [
            {"id": n, "keys": [name], "content": f"The {name}.", "extensions": {"display_index": n}}
            for n, name in enumerate(["farmhouse", "creek", "ridge"])
        ]
    }
    form = book_form(book)
    changes: list[bool] = []
    form.changed.connect(lambda: changes.append(True))
    form.list.setCurrentRow(2)
    assert form.up_button.isEnabled() and not form.down_button.isEnabled(), "the last"
    form.up_button.click()
    assert shown_names(form) == ["farmhouse", "ridge", "creek"]
    assert [e["keys"][0] for e in book["entries"]] == ["farmhouse", "ridge", "creek"]
    assert form.list.currentRow() == 1 and form._current() is book["entries"][1]
    assert [e["extensions"]["display_index"] for e in book["entries"]] == [0, 1, 2]
    assert '"display_index": 1' in form.entry_rest.text.toPlainText(), "the JSON box in step"
    # The keys, with the list focused.
    form.show()
    form.activateWindow()
    QTest.qWaitForWindowActive(form, 2000)
    form.list.setFocus()
    QTest.keyClick(form.list, Qt.Key_Up, Qt.AltModifier)
    assert shown_names(form) == ["ridge", "farmhouse", "creek"] and form.list.currentRow() == 0
    assert not form.up_button.isEnabled() and form.down_button.isEnabled(), "the first"
    QTest.keyClick(form.list, Qt.Key_Up, Qt.AltModifier)
    assert shown_names(form) == ["ridge", "farmhouse", "creek"], "no further"
    QTest.keyClick(form.list, Qt.Key_Down, Qt.AltModifier)
    QTest.keyClick(form.list, Qt.Key_Down, Qt.AltModifier)
    assert shown_names(form) == ["farmhouse", "creek", "ridge"] and form.list.currentRow() == 2
    assert form._current()["keys"] == ["ridge"] and len(changes) == 4
    # A search hiding some entries stops them (they would pass unseen), and says so.
    form.list.setCurrentRow(1)
    form.find.setText("creek")
    assert not form.up_button.isEnabled() and not form.down_button.isEnabled()
    assert form.up_button.toolTip() == form.down_button.toolTip() == SEARCH_HIDES
    QTest.keyClick(form.list, Qt.Key_Up, Qt.AltModifier)
    assert shown_names(form) == ["farmhouse", "creek", "ridge"]
    form.find.setText("the")  # every entry: none hidden
    assert form.up_button.isEnabled() and form.up_button.toolTip() == MOVE_UP_TIP
    form.find.clear()
    form.close()


def test_sillytaverns_own_shape_moves_by_its_display_index(app):
    book = {
        "entries": {
            "0": {"uid": 0, "key": ["farmhouse"], "content": "a", "displayIndex": 0},
            "1": {"uid": 1, "key": ["creek"], "content": "b", "displayIndex": 1},
            "2": {"uid": 2, "key": ["ridge"], "content": "c"},
        }
    }
    form = book_form(book)
    form.list.setCurrentRow(1)
    form.down_button.click()
    assert shown_names(form) == ["farmhouse", "ridge", "creek"]
    assert [book["entries"][n]["displayIndex"] for n in ("0", "2", "1")] == [0, 1, 2]
    assert form._current() is book["entries"]["1"]
    assert '"displayIndex": 2' in form.entry_rest.text.toPlainText()
    assert list(book["entries"]) == ["0", "1", "2"], "each kept under its own number"


def test_your_characters_take_another_apps_lorebook_and_say_what_it_lost(
    app, window, tmp_path, monkeypatch, yes
):
    """A NovelAI lorebook on the Lorebook tab: converted as SillyTavern converts
    it, then put on the card as SillyTavern puts a lorebook file on one; what
    the conversion lost is said; moved, it is exported in its new order."""
    import json

    from tests.test_lorebook_formats import NOVEL_BOOK

    elena, page = shown(window)
    form = page.lorebook_form
    path = tmp_path / "doomsville.lorebook"
    path.write_text(json.dumps(NOVEL_BOOK))
    monkeypatch.setattr(
        window_collection.QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), "")
    )
    said = no_boxes(monkeypatch)
    form.import_button.click()
    entries = lore.book_entries(elena.tavern.character_book)
    assert [lore.entry_title(e) for e in entries] == ["Hellsville", "", "", ""]
    assert entries[0]["insertion_order"] == 400 and entries[0]["use_regex"] is True
    assert elena.tavern.character_book["name"] == "doomsville"
    assert len(said) == 1 and "NovelAI's Always On" in said[0] and "(1 entry)" in said[0]
    assert "4 entries are Elena's own lorebook now." in said[0]
    assert on_disk(window).tavern.character_book == elena.tavern.character_book, "kept"
    # Moved, it goes out in its new order.
    form.list.setCurrentRow(3)
    form.up_button.click()
    out = tmp_path / "elena lorebook.json"
    monkeypatch.setattr(
        window_collection.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), "")
    )
    form.export_button.click()
    world = json.loads(out.read_text(encoding="utf-8"))
    assert [e.get("content") for e in lore.entries_of(world)] == [
        "Hellsville is walled, and wary of strangers.",
        "Four winters after the end of the world.",
        "The ridge.",
        None,  # what was no entry at all in the NovelAI file has no text
    ]
    assert on_disk(window).tavern.character_book == elena.tavern.character_book


def test_your_characters_say_why_another_apps_lorebook_cant_be_taken(
    app, window, tmp_path, monkeypatch
):
    import json

    elena, page = shown(window)
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(text))
    for name, data in (
        ("broken.json", {"type": "risu", "data": [{"key": 7}]}),
        ("odd.json", {"entries": "none"}),
    ):
        path = tmp_path / name
        path.write_text(json.dumps(data))
        monkeypatch.setattr(
            window_collection.QFileDialog, "getOpenFileName", lambda *a, p=path, **k: (str(p), "")
        )
        page.lorebook_form.import_button.click()
    assert warned == [
        "broken.json is a RisuAI lorebook that SillyTavern can't import either: entry 1's "
        "keywords aren't text.",
        "odd.json has no lorebook.",  # none of these shapes: the words it always had
    ]
    assert lore.book_entries(elena.tavern.character_book)[0]["keys"] == ["Hellsville", "the town"]


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


def test_a_lorebook_goes_out_and_comes_in_with_every_setting(
    app, window, tmp_path, monkeypatch, yes
):
    import json

    from tests.test_card_editing import ST_CARD_BOOK

    elena, page = shown(window)
    elena.tavern.character_book = json.loads(json.dumps(ST_CARD_BOOK))
    page.show_book()
    out = tmp_path / "elena lorebook.json"
    monkeypatch.setattr(
        window_collection.QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), "")
    )
    page.lorebook_form.export_button.click()
    world = json.loads(out.read_text(encoding="utf-8"))
    assert world["originalData"] == ST_CARD_BOOK
    assert world["entries"]["7"]["probability"] == 70 and world["entries"]["7"]["depth"] == 2
    # Back in, onto the card, as SillyTavern would put it there.
    elena.tavern.character_book = None
    monkeypatch.setattr(
        window_collection.QFileDialog, "getOpenFileName", lambda *a, **k: (str(out), "")
    )
    page.lorebook_form.import_button.click()
    entry = lore.book_entries(elena.tavern.character_book)[0]
    assert entry["extensions"]["group"] == "towns" and entry["insertion_order"] == 40
    assert page.lorebook_form.list.count() == 1


def test_a_picture_taken_off_a_card_leaves_the_disk_too(app, window, yes):
    elena, page = shown(window)
    store = DiskPictures(COLLECTION_ID, window.root)
    ref = elena.reference_images[0]
    assert store.read(ref.file) is not None
    window._collection_board_picture("remove", elena.id, ref.id)
    assert ref not in elena.reference_images
    assert store.read(ref.file) is None, "no character has it any more"


def test_a_picture_another_character_still_has_stays(app, window, yes):
    elena, page = shown(window)
    store = DiskPictures(COLLECTION_ID, window.root)
    window._duplicate_kept(elena.id)
    copy = next(c for c in window._kept() if c is not elena)
    shared = elena.reference_images[0]
    copy.reference_images.append(shared.model_copy())  # as if both kept it
    window._collection_pictures_changed(copy.id)
    window._collection_board_picture("remove", elena.id, shared.id)
    assert store.read(shared.file) is not None, "the copy still shows it"


def test_the_files_tab_badges_each_file_by_its_kind(app, window):
    from sealedlore.gui.card_files_form import badge

    elena, page = shown(window)
    form = page.files_form
    by_name = {form.files.item(r).text(): form.files.item(r) for r in range(form.files.count())}
    assert by_name["wind"].icon().cacheKey() == badge("mp3").cacheKey()
    assert by_name["you"].icon().cacheKey() == badge("LINK", accent=True).cacheKey()
    assert badge("mp3").cacheKey() != badge("LINK", accent=True).cacheKey()


def test_the_list_fills_in_portraits_as_they_are_read(app, window):
    import time

    elena, page = shown(window)
    page._fill()
    item = next(
        page.people.item(r)
        for r in range(page.people.count())
        if page.people.item(r).text().startswith("Elena")
    )
    end = time.monotonic() + 3
    while not item.data(Qt.UserRole + 11) and time.monotonic() < end:
        app.processEvents()
    assert item.data(Qt.UserRole + 11), "read a few a tick, and shown once read"
