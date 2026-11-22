"""Always-on lore in the window (gui/window_always_on.py,
gui/always_on_dialog.py): the Lore page says when it is more than the lore
budget, the Always-on entries dialog makes entries optional and writes
condensed versions, an entry's own is shown and edited on its page, and a
kept lorebook's is written from Lorebooks. Offscreen, the mock provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from sealedlore.gui import lorebooks_page
from sealedlore.gui.always_on_dialog import AlwaysOnDialog
from sealedlore.gui.main_window import MainWindow
from sealedlore.models.lore import LoreEntry, lore_fingerprint
from sealedlore.providers.mock import MockChatProvider
from sealedlore.storage.lorebooks import load_lorebooks
from sealedlore.storage.repository import StoryBundle, load_story_bundle, save_story_bundle
from sealedlore.storage.tavern_lore import condensed_key


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def long_entry(title: str, words: int = 1500, **fields) -> LoreEntry:
    return LoreEntry(title=title, content=" ".join([title.lower()] * words), **fields)


def window_with(tmp_path: Path, story, cast, lore) -> MainWindow:
    story.held_character_id = "char-serrik"
    save_story_bundle(StoryBundle(story=story, cast=list(cast), lore=lore), root=tmp_path / "data")
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    window.open_story(story.id)
    # Too big to go whole (a tenth of 20k): chosen from, the always-on standing.
    window.session.story.defaults.context_token_budget = 20_000
    window.config.lore_limit = "tokens"
    window.config.lore_token_cap = 1_000
    window._reassemble_inspector()
    return window


def test_the_lore_page_says_when_always_on_lore_is_over_the_budget(app, tmp_path, story, cast):
    window = window_with(
        tmp_path,
        story,
        cast,
        [long_entry("Keep", always_on=True), long_entry("Gate", always_on=True)],
    )
    note = window.lore_panel.always_on_note
    assert not note.isHidden()
    assert "more than the lore budget of 1,000" in note.text() and "condensed" in note.text()
    assert not window.lore_panel.always_on_button.isHidden()
    window.config.lore_token_cap = 100_000
    window._reassemble_inspector()
    assert note.isHidden()
    window.close()


def test_the_dialog_makes_entries_optional_and_says_what_they_come_to(
    app, tmp_path, story, cast, monkeypatch
):
    keep = long_entry("Keep", always_on=True, keywords=["keep"])
    gate = long_entry("Gate", always_on=True)  # no keywords
    window = window_with(tmp_path, story, cast, [keep, gate])
    seen: dict[str, str] = {}

    def exec_(dialog: AlwaysOnDialog) -> int:
        assert "more than the budget" in dialog.said.text().lower()
        dialog.table.item(0, 0).setCheckState(Qt.Unchecked)  # Keep optional
        dialog.table.item(1, 0).setCheckState(Qt.Unchecked)
        seen["keep"] = dialog.table.item(0, 4).text()
        seen["gate"] = dialog.table.item(1, 4).text()
        seen["said"] = dialog.said.text()
        dialog.use_condensed.setChecked(False)
        return QDialog.Accepted

    monkeypatch.setattr(AlwaysOnDialog, "exec", exec_)
    window.open_always_on()
    assert seen["keep"] == "Optional: sent when its keywords come up"
    assert "no keywords: it would never be sent" in seen["gate"]
    assert "0 of 2 entries always on" in seen["said"]
    again = load_story_bundle(story.id, root=tmp_path / "data")
    assert [e.always_on for e in again.lore] == [False, False]
    assert again.story.defaults.condense_lore is False
    assert window.lore_panel.always_on_note.isHidden()
    window.close()


def test_writing_condensed_versions_from_the_dialog_and_sending_them(
    app, tmp_path, story, cast, monkeypatch
):
    keep, gate = long_entry("Keep", always_on=True), long_entry("Gate", always_on=True)
    window = window_with(tmp_path, story, cast, [keep, gate])
    monkeypatch.setattr(
        window,
        "_current_provider",
        lambda: MockChatProvider(["A short keep.", "A short gate."]),
    )
    rows: list[str] = []

    def exec_(dialog: AlwaysOnDialog) -> int:
        assert dialog.condense_button.isEnabled()
        dialog.condense_button.click()
        rows.extend(dialog.table.item(row, 4).text() for row in range(2))
        rows.append(dialog.said.text())
        return QDialog.Accepted

    monkeypatch.setattr(AlwaysOnDialog, "exec", exec_)
    window.open_always_on()
    session = window.session
    assert [e.condensed_now() for e in session.bundle.lore] == ["A short keep.", "A short gate."]
    assert "Sent condensed" in rows and "sent condensed" in rows[-1]
    # The entries themselves are as they were, and both kept on disk.
    again = load_story_bundle(story.id, root=tmp_path / "data")
    assert again.lore[0].content.startswith("keep keep") and again.lore[0].condensed_now()
    assert "sent condensed" in window.lore_panel.always_on_note.text()
    window.close()


def test_an_entrys_condensed_version_on_its_page(app, tmp_path, story, cast, monkeypatch):
    keep = long_entry("Keep", always_on=True)
    window = window_with(tmp_path, story, cast, [keep])
    panel = window.lore_panel
    panel.select_entry(keep.id)
    assert panel._form.isRowVisible(panel.condensed)
    assert panel.condense_button.text() == "Write a condensed version"
    monkeypatch.setattr(window, "_current_provider", lambda: MockChatProvider(["A keep."]))
    panel.condense_button.click()
    entry = window.session.bundle.lore[0]
    assert entry.condensed_now() == "A keep."
    panel.select_entry(keep.id)
    assert panel.condensed.toPlainText() == "A keep."
    assert panel.condense_button.text() == "Write it again"
    # The author's own words are current for the entry as it stands.
    panel.condensed.setPlainText("My own, shorter.")
    assert entry.condensed_now() == "My own, shorter."
    # An edit to the entry leaves it out of date, and says so.
    panel.content.setPlainText(entry.content + " and more")
    assert entry.condensed_now() is None and "Out of date" in panel.condensed_state.text()
    # An entry neither always on nor condensed shows no such rows.
    panel.always_on.setChecked(False)
    panel.condensed.setPlainText("")
    assert not panel._form.isRowVisible(panel.condensed)
    window.close()


def test_a_kept_lorebook_is_condensed_from_lorebooks_and_its_stories_start_whole(
    app, tmp_path, monkeypatch
):
    book = {
        "entries": {
            "0": {
                "uid": 0,
                "comment": "The Valley",
                "constant": True,
                "key": [],
                "content": "@@activate\n" + "the valley " * 200,
            },
            "1": {"uid": 1, "comment": "Short", "constant": True, "key": [], "content": "Brief."},
            "2": {"uid": 2, "comment": "Wolves", "key": ["wolf"], "content": "wolf " * 300},
        }
    }
    path = tmp_path / "valley.json"
    path.write_text(json.dumps(book), encoding="utf-8")
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    monkeypatch.setattr(
        "sealedlore.gui.file_dialogs.open_file_names", lambda *a, **k: ([str(path)], "")
    )
    window.show_lorebooks(True)
    window.lorebooks_page.action_requested.emit(lorebooks_page.IMPORT, "")
    (stored,) = window._lorebooks()
    # Your characters' own client writes them, never the story's.
    window._collection_session().provider = MockChatProvider(["A valley."])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.lorebooks_page.action_requested.emit(lorebooks_page.CONDENSE, stored.id)
    (saved,) = load_lorebooks(tmp_path / "data").books
    assert saved.condensed == {condensed_key(book["entries"]["0"]): "A valley."}
    assert saved.book["entries"]["0"]["content"].startswith("@@activate")  # the book as it came
    # Its entry form shows it, beside the whole text.
    form = window.lorebooks_page.form
    form.list.setCurrentRow(0)
    assert form._entry_form.isRowVisible(form.condensed)
    assert form.condensed.toPlainText() == "A valley."
    form.list.setCurrentRow(2)  # not always sent, none: no row
    assert not form._entry_form.isRowVisible(form.condensed)
    # A story made from it has both, and starts with the whole text.
    from sealedlore.storage.lorebooks import scenario_from

    scenario, _ = scenario_from(saved, title="T")
    valley = scenario.lore[0]
    assert valley.content.startswith("the valley") and valley.condensed_now() == "A valley."
    window.close()


def test_a_storys_lore_kept_in_lorebooks_takes_its_condensed_versions(app, tmp_path, story, cast):
    keep = long_entry("Keep", always_on=True)
    keep.condensed, keep.condensed_from = "A keep.", lore_fingerprint(keep.content)
    window = window_with(tmp_path, story, cast, [keep])
    window.story_lore_to_lorebooks()
    (kept,) = load_lorebooks(tmp_path / "data").books
    from sealedlore.storage.lorebooks import lore_of

    assert lore_of(kept).entries[0].condensed_now() == "A keep."
    window.close()
