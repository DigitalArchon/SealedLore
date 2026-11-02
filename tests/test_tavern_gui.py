"""SillyTavern cards and lorebooks in the window: a new story from a card
(File → Import…), a card into the open story and out of it (the Cast page),
and lorebooks both ways (the Lore page)."""

from __future__ import annotations

import io
import json
import os
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton  # noqa: E402

import sealedlore.gui.window_tavern as window_tavern  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.tavern_dialog import NO_OPENING, CardImportDialog, LinkDialog  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.providers.card_links import Fetched, LinkError  # noqa: E402
from sealedlore.storage.png_text import text_chunks  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from sealedlore.storage.tavern_cards import read_card  # noqa: E402
from sealedlore.storage.tavern_lore import (  # noqa: E402
    book_from_lore,
    read_lorebook,
    write_lorebook,
)
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


# --- by link ---------------------------------------------------------------------------


def wait_for(app: QApplication, condition, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        app.processEvents()
        time.sleep(0.002)


def paste_link(monkeypatch, link: str) -> None:
    def accepted(dialog: LinkDialog) -> int:
        dialog.field.setText(link)
        assert dialog.import_button.isEnabled()
        return QDialog.Accepted

    monkeypatch.setattr(LinkDialog, "exec", accepted)


def answered_with(monkeypatch, answer, gate: threading.Event | None = None) -> list[str]:
    """The fetch, answered on its thread (after `gate`, when given); the links asked."""
    asked: list[str] = []

    def fetch(link: str, *, stop=None, transport=None):
        asked.append(link)
        if gate is not None:
            gate.wait(10)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(window_tavern, "fetch_link", fetch)
    return asked


def a_card(picture: bytes | None = None) -> Fetched:
    return Fetched(io.BytesIO(json.dumps(v2()).encode()), "elena.json", "Chub", picture=picture)


def test_the_link_dialog_says_what_can_t_be_fetched_before_sending(app):
    dialog = LinkDialog("Import from a link", "Paste the link:")
    assert not dialog.import_button.isEnabled()
    dialog.field.setText("https://janitorai.com/characters/1234_character-elena")
    assert not dialog.import_button.isEnabled() and "JanitorAI" in dialog.problem.text()
    dialog.field.setText("https://chub.ai/characters/someone/elena-1234")
    assert dialog.import_button.isEnabled() and dialog.problem.isHidden()
    dialog.deleteLater()


def test_a_card_by_link_makes_a_story(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    paste_link(monkeypatch, "https://chub.ai/characters/someone/elena-1234")
    asked = answered_with(monkeypatch, a_card(picture=tiny_png()))

    def accepted(dialog) -> int:
        dialog.player.setText("John")
        return QDialog.Accepted

    monkeypatch.setattr(CardImportDialog, "exec", accepted)
    no_boxes(monkeypatch)
    monkeypatch.setattr(window, "open_setup", lambda: False)

    window.import_from_link()
    wait_for(app, lambda: window.session is not None)

    assert asked == ["https://chub.ai/characters/someone/elena-1234"]
    assert [c.name for c in window.session.cast] == ["Elena", "John"]
    portrait = window.session.cast[0].reference_images[0]
    assert window.session.pictures.read(portrait.file)
    assert window._link_fetch is None and window._link_progress is None
    window.close()


def test_a_card_by_link_joins_the_open_story(app, tmp_path: Path, monkeypatch, story, cast):
    window = window_on(tmp_path, story, cast)
    paste_link(monkeypatch, "https://files.catbox.moe/elena.json")
    answered_with(monkeypatch, a_card())
    monkeypatch.setattr(window, "_ask_where_card_goes", lambda character, lore: (True, False))
    no_boxes(monkeypatch)

    window.cast_panel.card_link_requested.emit()
    wait_for(app, lambda: any(c.name == "Elena" for c in window.session.cast))
    assert "Hellsville" not in [entry.title for entry in window.session.bundle.lore]
    window.close()


def test_a_lorebook_by_link_goes_into_the_lore(app, tmp_path: Path, monkeypatch, story, cast):
    window = window_on(tmp_path, story, cast)
    book = book_from_lore([LoreEntry(title="The ridge", content="Cold.", keywords=["ridge"])])
    paste_link(monkeypatch, "https://chub.ai/lorebooks/someone/the-ridge")
    answered_with(
        monkeypatch,
        Fetched(io.BytesIO(json.dumps(book).encode()), "the-ridge.json", "Chub", lorebook=True),
    )
    no_boxes(monkeypatch)

    window.lore_panel.lorebook_link_requested.emit()
    wait_for(app, lambda: window.session.bundle.lore)
    assert [entry.title for entry in window.session.bundle.lore] == ["The ridge"]

    # A card's own lorebook by link, from the Lore page.
    answered_with(monkeypatch, a_card())
    window.lore_panel.lorebook_link_requested.emit()
    wait_for(app, lambda: len(window.session.bundle.lore) == 2)
    assert window.session.bundle.lore[-1].title == "Hellsville"
    window.close()


def test_a_lorebook_by_link_with_no_story_says_where_it_goes(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    book = book_from_lore([LoreEntry(title="The ridge", content="Cold.")])
    paste_link(monkeypatch, "https://chub.ai/lorebooks/someone/the-ridge")
    answered_with(monkeypatch, Fetched(io.BytesIO(json.dumps(book).encode()), "b.json", "Chub"))
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(text))
    window.import_from_link()
    wait_for(app, lambda: warned)
    assert "Open the story it is for" in warned[0] and window.session is None
    window.close()


def test_a_link_that_fails_says_why(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    paste_link(monkeypatch, "https://chub.ai/characters/someone/gone")
    answered_with(monkeypatch, LinkError("Chub has nothing at that address."))
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(text))
    window.import_from_link()
    wait_for(app, lambda: warned)
    assert warned == ["Chub has nothing at that address."]
    assert window._link_fetch is None and window.session is None
    window.close()


def test_stop_drops_what_the_link_brings_later(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    paste_link(monkeypatch, "https://chub.ai/characters/someone/elena-1234")
    gate = threading.Event()
    late = a_card()
    answered_with(monkeypatch, late, gate)
    window.import_from_link()
    progress = window._link_progress
    assert progress is not None and progress.isVisible()
    # Its own button: cancel() from code doesn't emit canceled.
    progress.findChild(QPushButton).click()
    assert window._link_fetch is None and not progress.isVisible()
    gate.set()
    wait_for(app, lambda: late.file.closed)
    assert window.session is None
    window.close()


def test_the_start_page_starts_from_a_card_s_link(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    shown: list[str] = []

    def rejected(dialog: LinkDialog) -> int:
        shown.append(dialog.windowTitle())
        return QDialog.Rejected

    monkeypatch.setattr(LinkDialog, "exec", rejected)
    window.start_page.cards["link"].activated.emit()
    assert shown == ["Import from a link"] and window._link_fetch is None
    window.close()
