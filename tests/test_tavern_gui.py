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
from sealedlore.providers.card_links import Fetched, LinkError, LinkLarge  # noqa: E402
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
        "expressions": False,
    }
    dialog.deleteLater()


# --- a new story from a card -------------------------------------------------------


def test_file_import_keeps_a_card_and_a_story_is_made_from_it_there(
    app, tmp_path: Path, monkeypatch
):
    """A card read in is kept in Your characters (the author, Nov 4 2026: it
    used to become a story at once); its page makes the story it would have."""
    path = png_card(tmp_path, {"chara": v2()})
    window = window_on(tmp_path)
    choose_file(monkeypatch, path)
    said = no_boxes(monkeypatch)

    window.import_file()

    assert window.session is None and window.collection_shown()
    elena = window.collection_page.current_character()
    assert elena.name == "Elena" and elena.collection.came_from == "card.png"
    assert elena.full_description.startswith("{{char}} keeps")  # names go in later
    assert elena.tavern.first_mes == ELENA["first_mes"]

    def accepted(dialog) -> int:
        dialog.player.setText("John")
        return QDialog.Accepted

    monkeypatch.setattr(CardImportDialog, "exec", accepted)
    set_up: list[bool] = []
    monkeypatch.setattr(window, "open_setup", lambda: set_up.append(True) or False)
    window.collection_page.story_action.trigger()

    session = window.session
    assert session is not None and session.story.title == "Elena"
    assert not window.collection_shown()
    assert [c.name for c in session.cast] == ["Elena", "John"]
    assert "She trusts John slowly." in session.cast[0].full_description
    assert session.story.setup.opening_text.startswith("Elena lowers the rifle")
    assert [entry.title for entry in session.bundle.lore] == ["Hellsville"]
    portrait = session.cast[0].reference_images[0]
    assert text_chunks(session.pictures.read(portrait.file)) == {}
    assert set_up == [True]
    assert any("1 other greeting" in words for words in said)
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


def test_other_apps_lorebooks_go_into_the_lore_as_sillytavern_converts_them(
    app, tmp_path: Path, monkeypatch, story, cast
):
    """NovelAI's (a .lorebook, and a picture with one in it), Agnai's and
    RisuAI's, on the Lore page: what the conversion lost said with the rest."""
    from tests.test_lorebook_formats import AGNAI_BOOK, NOVEL_BOOK, RISU_BOOK, novel_picture

    window = window_on(tmp_path, story, cast)
    books = {"dust.lorebook": NOVEL_BOOK, "agnai.json": AGNAI_BOOK, "risu.json": RISU_BOOK}
    files = []
    for name, book in books.items():
        files.append(tmp_path / name)
        files[-1].write_text(json.dumps(book))
    files.append(novel_picture(tmp_path, NOVEL_BOOK))
    said = no_boxes(monkeypatch)
    for path in files:
        choose_file(monkeypatch, path)
        window.lore_panel.lorebook_import_requested.emit()
    novel = ["Hellsville", "Entry 2", "ridge"]
    assert [entry.title for entry in window.session.bundle.lore] == [
        *novel,
        "The farmhouse",
        "creek",
        "Entry 3",
        "Hellsville",
        "The world",
        "creek",
        *novel,
    ]
    assert [entry.always_on for entry in window.session.bundle.lore][6:8] == [False, True]
    assert len(said) == 3, "the Agnai book lost nothing and left nothing out"
    assert said[0].startswith("Added 3 lore entries from dust.lorebook.")
    assert "NovelAI's Always On" in said[0] and "regular expressions" in said[0]
    assert "second keywords" in said[1] and "a chance below 100%" in said[1]
    assert said[2].startswith("Added 3 lore entries from doomsville.png.")
    window.close()


def test_file_import_takes_another_apps_lorebook_as_one(
    app, tmp_path: Path, monkeypatch, story, cast
):
    """An Agnai book with a description reads as an old card too: File →
    Import… tells it as SillyTavern's lorebook import would, first."""
    from sealedlore.storage.tavern_cards import tavern_kind
    from tests.test_lorebook_formats import AGNAI_BOOK, NOVEL_BOOK, novel_picture

    agnai = tmp_path / "doomsville.json"
    agnai.write_text(json.dumps(AGNAI_BOOK))
    assert tavern_kind(agnai) == "card"
    novel = tmp_path / "dust.lorebook"
    novel.write_text(json.dumps(NOVEL_BOOK))
    window = window_on(tmp_path, story, cast)
    no_boxes(monkeypatch)
    for path in (agnai, novel, novel_picture(tmp_path, NOVEL_BOOK)):
        choose_file(monkeypatch, path)
        window.import_file()
    assert [entry.title for entry in window.session.bundle.lore][:4] == [
        "The farmhouse",
        "creek",
        "Entry 3",
        "Hellsville",
    ]
    assert len(window.session.bundle.lore) == 9 and not window.collection_shown()
    window.close()

    window = window_on(tmp_path / "two")
    choose_file(monkeypatch, novel)
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(text))
    window.import_file()
    assert warned == [
        "dust.lorebook is a lorebook. Open the story it is for, then import it again (or from "
        "the Lore page)."
    ]
    window.close()


def test_what_is_no_lorebook_keeps_its_words_and_a_broken_one_says_why(
    app, tmp_path: Path, monkeypatch, story, cast
):
    """A file of none of the shapes is refused as a JSON file always was; one
    marked as another app's that SillyTavern couldn't convert either says why."""
    window = window_on(tmp_path, story, cast)
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(text))
    for name, text in (
        ("odd.json", '{"entries": "none"}'),
        ("dust.lorebook", '{"title": "Doomsville"}'),
        ("broken.lorebook", '{"lorebookVersion": 5, "entries": [null]}'),
    ):
        (tmp_path / name).write_text(text)
        choose_file(monkeypatch, tmp_path / name)
        window.import_lorebook()
    assert warned == [
        "odd.json isn't a SillyTavern lorebook.",
        "dust.lorebook isn't a SillyTavern lorebook.",
        "broken.lorebook is a NovelAI lorebook that SillyTavern can't import either: entry 1 "
        "is empty.",
    ]
    assert window.session.bundle.lore == []
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

    def fetch(link: str, *, stop=None, large_ok=False, transport=None):
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


def test_a_card_by_link_is_kept_in_your_characters(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    link = "https://chub.ai/characters/someone/elena-1234"
    paste_link(monkeypatch, link)
    asked = answered_with(monkeypatch, a_card(picture=tiny_png()))
    no_boxes(monkeypatch)

    window.import_from_link()
    wait_for(app, lambda: window._collection is not None and window._collection.cast)

    assert asked == [link] and window.session is None
    elena = window._collection.cast[0]
    assert elena.name == "Elena" and elena.collection.came_from == link
    assert window._collection.pictures.read(elena.reference_images[0].file)
    assert window.collection_shown()
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


def test_another_apps_lorebook_by_link_is_converted_too(
    app, tmp_path: Path, monkeypatch, story, cast
):
    from tests.test_lorebook_formats import RISU_BOOK

    window = window_on(tmp_path, story, cast)
    paste_link(monkeypatch, "https://files.catbox.moe/doomsville.json")
    answered_with(
        monkeypatch,
        Fetched(io.BytesIO(json.dumps(RISU_BOOK).encode()), "doomsville.json", "catbox"),
    )
    said = no_boxes(monkeypatch)
    window.lore_panel.lorebook_link_requested.emit()
    wait_for(app, lambda: window.session.bundle.lore)
    assert [entry.title for entry in window.session.bundle.lore] == [
        "Hellsville",
        "The world",
        "creek",
    ]
    assert "second keywords" in said[0]
    # One SillyTavern's converter stops on says why.
    broken = {"type": "risu", "data": [{"key": 7, "content": "c"}]}
    answered_with(
        monkeypatch, Fetched(io.BytesIO(json.dumps(broken).encode()), "broken.json", "catbox")
    )
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(text))
    window.lore_panel.lorebook_link_requested.emit()
    wait_for(app, lambda: warned)
    assert warned == [
        "broken.json is a RisuAI lorebook that SillyTavern can't import either: entry 1's "
        "keywords aren't text."
    ]
    assert len(window.session.bundle.lore) == 3
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


def test_the_start_page_opens_your_characters(app, tmp_path: Path):
    window = window_on(tmp_path)
    window.start_page.cards["collection"].activated.emit()
    assert window.collection_shown()
    assert window.collection_page.back_button.text() == "Close"
    window.close()


def test_a_large_card_is_downloaded_only_on_a_yes(app, tmp_path: Path, monkeypatch):
    window = window_on(tmp_path)
    paste_link(monkeypatch, "https://realm.risuai.net/character/7adb0ed8-d818")
    calls: list[bool] = []

    def fetch(link: str, *, stop=None, large_ok=False, transport=None):
        calls.append(large_ok)
        if not large_ok:
            raise LinkLarge(82 * 1024 * 1024)
        return a_card()

    monkeypatch.setattr(window_tavern, "fetch_link", fetch)
    said: list[str] = []

    def exec_(box) -> int:
        said.append(" | ".join([box.text(), *(b.text() for b in box.buttons())]))
        return QMessageBox.Cancel

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    window.import_from_link()
    wait_for(app, lambda: said)
    assert calls == [False] and window._link_fetch is None and window.session is None
    assert "This card's file is 82 MB." in said[0] and "Download 82 MB" in said[0]

    monkeypatch.setattr(window, "_download_large_card", lambda size: True)
    no_boxes(monkeypatch)
    window.import_from_link()
    wait_for(app, lambda: window._collection is not None and window._collection.cast)
    assert calls == [False, False, True]
    window.close()
