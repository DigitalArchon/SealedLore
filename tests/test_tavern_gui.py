"""SillyTavern cards and lorebooks in the window: a new story from a card
(File → Import…), a card into the open story and out of it (the Cast page),
and lorebooks both ways (the Lore page)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

import sealedlore.gui.window_tavern as window_tavern  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.tavern_dialog import NO_OPENING, CardImportDialog  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.storage.png_text import text_chunks  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from sealedlore.storage.tavern_cards import read_card  # noqa: E402
from sealedlore.storage.tavern_lore import read_lorebook, write_lorebook  # noqa: E402
from tests.test_tavern import ELENA, png_card, tiny_png, v2  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def window_on(tmp_path: Path, story=None, cast=()) -> MainWindow:
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    if story is not None:
        save_story_bundle(StoryBundle(story=story, cast=list(cast)), root=tmp_path / "data")
        window.open_story(story.id)
    return window


def choose_file(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(
        "sealedlore.gui.main_window.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(path), ""),
    )
    monkeypatch.setattr(
        "sealedlore.gui.window_tavern.QFileDialog.getOpenFileName",
        lambda *args, **kwargs: (str(path), ""),
    )


def save_to(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(
        "sealedlore.gui.window_tavern.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(path), ""),
    )


def no_boxes(monkeypatch) -> list[str]:
    """Every information box answered, and what it said kept."""
    said: list[str] = []

    def exec_(box) -> int:
        said.append(f"{box.text()} {box.informativeText()}")
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    return said


# --- the dialog ------------------------------------------------------------------


def test_the_dialog_asks_who_you_play_before_it_makes_anything(app, tmp_path: Path):
    dialog = CardImportDialog(read_card(png_card(tmp_path, {"chara": v2()})))
    ok = dialog.buttons.buttons()[0]
    assert not ok.isEnabled()
    dialog.player.setText("John")
    assert ok.isEnabled()
    assert dialog.preview.toPlainText() == 'Elena lowers the rifle an inch. "Who sent you, John?"'
    dialog.opening.setCurrentIndex(dialog.opening.findText(NO_OPENING))
    dialog.as_world.setChecked(True)
    dialog.instructions.setChecked(True)
    assert dialog.options() == {
        "player": "John",
        "about_player": "",
        "opening": None,
        "world": True,
        "instructions": True,
    }
    dialog.deleteLater()


# --- a new story from a card -------------------------------------------------------


def test_file_import_makes_a_story_from_a_card(app, tmp_path: Path, monkeypatch):
    path = png_card(tmp_path, {"chara": v2()})
    window = window_on(tmp_path)
    choose_file(monkeypatch, path)

    def accepted(dialog) -> int:
        dialog.player.setText("John")
        return QDialog.Accepted

    monkeypatch.setattr(CardImportDialog, "exec", accepted)
    said = no_boxes(monkeypatch)
    set_up: list[bool] = []
    monkeypatch.setattr(window, "open_setup", lambda: set_up.append(True) or False)

    window.import_file()

    session = window.session
    assert session is not None and session.story.title == "Elena"
    assert [c.name for c in session.cast] == ["Elena", "John"]
    assert session.story.setup.opening_text.startswith("Elena lowers the rifle")
    assert [entry.title for entry in session.bundle.lore] == ["Hellsville"]
    portrait = session.cast[0].reference_images[0]
    assert text_chunks(session.pictures.read(portrait.file)) == {}
    assert set_up == [True]
    assert said and "1 other greeting" in said[0]
    window.close()


def test_a_lorebook_by_file_import_needs_a_story(app, tmp_path: Path, monkeypatch, story, cast):
    path = tmp_path / "book.json"
    write_lorebook(path, [LoreEntry(title="The ridge", content="Cold.", keywords=["ridge"])])
    window = window_on(tmp_path)
    choose_file(monkeypatch, path)
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(text))
    window.import_file()
    assert warned and "Open the story it is for" in warned[0]
    window.close()

    window = window_on(tmp_path / "two", story, cast)
    choose_file(monkeypatch, path)
    no_boxes(monkeypatch)
    window.import_file()
    assert [entry.title for entry in window.session.bundle.lore] == ["The ridge"]
    window.close()


# --- a card into the open story, and out of it ---------------------------------------


def test_a_card_joins_the_open_story(app, tmp_path: Path, monkeypatch, story, cast):
    story.held_character_id = "char-serrik"
    window = window_on(tmp_path, story, cast)
    choose_file(monkeypatch, png_card(tmp_path, {"chara": v2()}))
    monkeypatch.setattr(window, "_ask_where_card_goes", lambda character, lore: (False, True))
    said = no_boxes(monkeypatch)

    window.cast_panel.card_import_requested.emit()

    session = window.session
    elena = session.supporting[-1]
    assert elena.name == "Elena" and elena not in session.cast
    assert "She trusts Serrik Vaun slowly." in elena.full_description
    assert elena.reference_images and session.pictures.read(elena.reference_images[0].file)
    assert [entry.title for entry in session.bundle.lore] == ["Hellsville"]
    assert said and "greetings belong to a story of their own" in said[0]
    window.close()


def test_a_character_goes_out_as_a_png_card_with_their_portrait(
    app, tmp_path: Path, monkeypatch, story, cast
):
    window = window_on(tmp_path, story, cast)
    session = window.session
    serrik = session.cast[0]
    session.pictures.write("images/refs/p.png", tiny_png())
    serrik.reference_images.append(ImageRef(id="p", file="images/refs/p.png"))
    session.bundle.lore.append(LoreEntry(title="The keep", content="Empty a hundred years."))
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)
    target = tmp_path / "serrik.png"
    save_to(monkeypatch, target)

    window.cast_panel.select_character(serrik.id)
    window.cast_panel.card_export_action.trigger()

    card = read_card(target)
    assert card.name == "Serrik Vaun" and card.spec == "chara_card_v3"
    assert [entry["comment"] for entry in card.book["entries"]] == ["The keep"]
    assert set(text_chunks(target.read_bytes())) == {"chara", "ccv3"}
    window.close()


def test_without_a_portrait_the_card_is_json(app, tmp_path: Path, monkeypatch, story, cast):
    window = window_on(tmp_path, story, cast)
    target = tmp_path / "maela.json"
    save_to(monkeypatch, target)
    window.export_card(window.session.cast[1].id)
    assert json.loads(target.read_text())["data"]["name"] == "Maela Orr"
    window.close()


def test_a_plot_hidden_card_is_not_offered_without_spoilers(
    app, tmp_path: Path, monkeypatch, story, cast
):
    window = window_on(tmp_path, story, cast)
    hidden = Character(name="The Stranger", plot_hidden=True)
    window.session.bundle.cast.append(hidden)
    monkeypatch.setattr(window.session, "hidden_ids", lambda: {hidden.id})
    window.config.show_plot_spoilers = False
    assert hidden not in window._shown_cards()
    window.config.show_plot_spoilers = True
    assert hidden in window._shown_cards()
    window.close()


# --- lorebooks on the Lore page -------------------------------------------------------


def test_the_lore_goes_out_as_a_lorebook_and_comes_back(
    app, tmp_path: Path, monkeypatch, story, cast
):
    window = window_on(tmp_path, story, cast)
    window.session.bundle.lore.append(
        LoreEntry(title="The keep", content="Empty.", keywords=["keep"])
    )
    target = tmp_path / "book.json"
    save_to(monkeypatch, target)
    window.lore_panel.lorebook_export_requested.emit()
    assert [entry["comment"] for entry in read_lorebook(target)["entries"].values()] == ["The keep"]

    choose_file(monkeypatch, target)
    no_boxes(monkeypatch)
    window.lore_panel.lorebook_import_requested.emit()
    assert [entry.title for entry in window.session.bundle.lore] == ["The keep", "The keep"]
    window.close()


def test_a_cards_own_lorebook_imports_as_lore(app, tmp_path: Path, monkeypatch, story, cast):
    window = window_on(tmp_path, story, cast)
    choose_file(monkeypatch, png_card(tmp_path, {"chara": v2()}))
    said = no_boxes(monkeypatch)
    window.import_lorebook()
    assert [entry.title for entry in window.session.bundle.lore] == ["Hellsville"]
    assert window.session.bundle.cast == window.session.cast
    assert not said
    window.close()


def test_a_card_with_no_lorebook_says_so(app, tmp_path: Path, monkeypatch, story, cast):
    window = window_on(tmp_path, story, cast)
    plain = {key: value for key, value in ELENA.items() if key != "character_book"}
    choose_file(monkeypatch, png_card(tmp_path, {"chara": v2(plain)}))
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(text))
    window.import_lorebook()
    assert warned == ["card.png has no lorebook in it."]
    assert window_tavern.CARD_FILTER.startswith("Character cards")
    window.close()
