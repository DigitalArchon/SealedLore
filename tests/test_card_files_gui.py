"""A card's files in the window: listed on Your characters' About tab,
saved or made one of the character's pictures; a chat with them showing the
pictures its greeting shows, its background behind it; a card with files
imported into a story kept whole in Your characters; and every file in the
CHARX a deleted card goes to the Trash as."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui import window_collection  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.transcript import ImageMessageWidget  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.storage import collection  # noqa: E402
from sealedlore.storage.paths import COLLECTION_ID  # noqa: E402
from sealedlore.storage.picture_store import DiskPictures  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from sealedlore.storage.tavern_cards import read_card  # noqa: E402
from tests.test_card_files import BARN, FARMHOUSE, WIND, risu_charx  # noqa: E402
from tests.test_card_link_gui import chat_dialog  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path):
    root = tmp_path / "data"
    bundle = collection.load_collection(root)
    elena, _notes = collection.kept_from_card(
        read_card(risu_charx(tmp_path)), DiskPictures(COLLECTION_ID, root)
    )
    bundle.cast.append(elena)
    save_story_bundle(bundle, root=root)
    john = Character(id="john", name="John")
    story = Story(id="story-1", title="Doomsville", held_character_id="john")
    story.scene.present_character_ids = ["john"]
    save_story_bundle(StoryBundle(story=story, cast=[john]), root=root)
    window = MainWindow(root=root, use_mock=True)
    window.open_story("story-1")
    yield window
    window._private_allows_close = lambda: True
    window.close()


def no_boxes(monkeypatch) -> list[str]:
    said: list[str] = []

    def exec_(box) -> int:
        said.append(f"{box.text()} {box.informativeText()}")
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    return said


def choose(about, name: str) -> None:
    for row in range(about.files.count()):
        item = about.files.item(row)
        if item.text() == name:
            about.files.setCurrentItem(item)
            item.setSelected(True)
            return
    raise AssertionError(f"no file named {name}")


def test_the_files_tab_lists_every_file_and_saves_one(app, window, tmp_path, monkeypatch):
    elena = window._kept()[0]
    window.show_collection(True, elena.id)
    about = window.collection_page.files_form
    names = [about.files.item(row).text() for row in range(about.files.count())]
    assert names == ["main", "Farmhouse", "snow", "wind", "you", "tiny"]
    assert "kept with the card" in about.said.text()
    choose(about, "you")  # a link: nothing to save
    assert not about.save_button.isEnabled()
    choose(about, "wind")
    assert about.save_button.isEnabled() and not about.picture_button.isEnabled()
    saved = tmp_path / "wind.mp3"
    monkeypatch.setattr(
        window_collection.QFileDialog, "getSaveFileName", lambda *a, **k: (str(saved), "")
    )
    about.save_button.click()
    assert saved.read_bytes() == WIND


def test_a_file_on_the_card_can_be_one_of_their_pictures(app, window):
    elena = window._kept()[0]
    window.show_collection(True, elena.id)
    about = window.collection_page.files_form
    before = len(elena.reference_images)
    choose(about, "Farmhouse")
    about.picture_button.click()
    added = elena.reference_images[-1]
    assert len(elena.reference_images) == before + 1 and added.caption == "Farmhouse"
    assert window._collection_store().read(added.file) == FARMHOUSE
    # The card keeps its own file.
    assert any(a.name == "Farmhouse" for a in elena.tavern.assets)


def test_a_chat_with_them_shows_its_greetings_pictures_and_background(app, window, monkeypatch):
    elena = window._kept()[0]
    chat_dialog(monkeypatch)
    window.chat_with_kept(elena.id)
    session = window.session
    _first, greeting = session.full_path()
    assert "{{img::" not in greeting.content and "<img" not in greeting.content
    assert greeting.content.startswith("Elena lowers the rifle an inch.")
    shown = [i for i in session.generated_images() if i.from_card]
    assert [i.card_file for i in shown] == ["Farmhouse", "snow"]
    assert all(i.anchor_node_id == greeting.id for i in shown)
    assert session.pictures.read(shown[0].file) == FARMHOUSE
    assert session.pictures.read(session.story.background_image) == BARN
    widgets = [
        w for w in window.transcript.findChildren(ImageMessageWidget) if w.image_id == shown[0].id
    ]
    assert widgets and widgets[-1].header.text() == "From Elena's card  ·  Farmhouse"
    actions = [a.text() for a in widgets[-1].more_button.menu().actions()]
    assert "Show the prompt" not in actions and "Generate again…" not in actions


def test_a_memory_chat_with_them_keeps_its_pictures_in_memory(app, window, monkeypatch):
    elena = window._kept()[0]
    before = sorted(p.relative_to(window.root) for p in window.root.rglob("*"))
    chat_dialog(monkeypatch, keep="memory")
    window.chat_with_kept(elena.id)
    session = window.session
    assert session.memory_only
    shown = [i for i in session.generated_images() if i.from_card]
    assert len(shown) == 2 and session.pictures.read(shown[0].file) == FARMHOUSE
    after = sorted(p.relative_to(window.root) for p in window.root.rglob("*"))
    assert [p for p in after if p not in before and p.parts[0] == "stories"] == []


def test_a_card_with_files_imported_into_a_story_is_kept_whole(app, window, tmp_path, monkeypatch):
    card = tmp_path / "again" / "elena.charx"
    card.parent.mkdir()
    card.write_bytes(risu_charx(tmp_path).read_bytes())
    monkeypatch.setattr(
        "sealedlore.gui.window_tavern.QFileDialog.getOpenFileName",
        lambda *a, **k: (str(card), ""),
    )
    monkeypatch.setattr(window, "_ask_where_card_goes", lambda character, lore: (True, False))
    said = no_boxes(monkeypatch)
    window.cast_panel.card_import_requested.emit()
    kept = window._kept()[-1]
    assert len(window._kept()) == 2 and len(kept.tavern.assets) == 6
    joined = next(c for c in window.session.cast if c.name == "Elena")
    assert joined.linked is not None and joined.linked.card_id == kept.id
    assert joined.tavern.assets == []
    assert said and "kept in Your characters too" in said[0]


def test_in_a_private_scene_a_card_with_files_waits(app, window, tmp_path, monkeypatch):
    monkeypatch.setattr(type(window.session), "in_private", property(lambda self: True))
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(text))
    window._card_into_story(read_card(risu_charx(tmp_path)))
    assert warned and "waits until the private scene ends" in warned[0]
    assert len(window._kept()) == 1 and [c.name for c in window.session.cast] == ["John"]


def test_a_deleted_cards_charx_has_every_file(app, window, tmp_path, monkeypatch):
    elena = window._kept()[0]
    trashed: list[bytes] = []
    monkeypatch.setattr(
        window_collection,
        "move_to_trash",
        lambda path: trashed.append(Path(path).read_bytes()) or True,
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    files = [a.file for a in elena.tavern.assets if a.held]
    window._delete_kept(elena.id)
    out = tmp_path / "trashed.charx"
    out.write_bytes(trashed[0])
    names = sorted(f.name for f in read_card(out).files)
    assert names == sorted(["main", "Farmhouse", "snow", "wind", "you", "tiny"])
    # Its files go with it.
    assert all(window._collection_store().read(file) is None for file in files)


def test_an_imported_card_says_what_its_files_are(app, window, tmp_path, monkeypatch):
    said = no_boxes(monkeypatch)
    window._card_into_collection(read_card(risu_charx(tmp_path)), "elena.charx")
    words = " ".join(said)
    assert "left out" not in words
    assert "1 background, 3 other pictures and 1 other file" in words
    page = window.collection_page
    assert page.files_form.files.count() == 6
    assert page.files_form.files.item(0).data(Qt.UserRole) == 0


def test_a_charx_with_every_file_is_offered_whatever_the_pictures(app, window):
    # The author: one portrait and eighteen files, and no CHARX on offer.
    elena = window._kept()[0]
    window.show_collection(True, elena.id)
    assert window.collection_page.charx_action.isEnabled()
    files = sum(1 for a in elena.tavern.assets if a.held)
    pictures = len(elena.reference_images)
    assert window_collection._everything(elena) == (
        f"{pictures} picture{'s' if pictures != 1 else ''} and {files} files"
    )


def test_a_click_on_a_picture_on_the_card_shows_it_full_size(app, window, monkeypatch):
    elena = window._kept()[0]
    window.show_collection(True, elena.id)
    about = window.collection_page.files_form
    shown: list[tuple[str, bytes]] = []
    monkeypatch.setattr(window, "_show_whole", lambda title, data: shown.append((title, data)))

    def click(name: str) -> None:
        item = next(
            about.files.item(r)
            for r in range(about.files.count())
            if about.files.item(r).text() == name
        )
        about.files.itemClicked.emit(item)

    click("Farmhouse")
    assert shown == [(f"{elena.name} · Farmhouse", FARMHOUSE)]
    click("wind")  # a sound: chosen, nothing to show
    click("you")  # a link: nothing here to show
    assert len(shown) == 1


def test_opening_your_characters_moves_files_kept_in_one_folder(app, window):
    root = window.root
    assert window._collection is None, "nothing opened them yet"
    bundle = collection.load_collection(root)
    elena = bundle.cast[0]
    pictures = DiskPictures(COLLECTION_ID, root)
    flat = []
    for number, asset in enumerate(a for a in elena.tavern.assets if a.held):
        data = pictures.read(asset.file)
        pictures.remove(asset.file)
        asset.file = f"{collection.FILES_DIR}/old{number}.{asset.ext}"
        pictures.write(asset.file, data)
        flat.append(asset.file)
    save_story_bundle(bundle, root=root)
    window._collection_session()
    again = collection.load_collection(root).cast[0]
    folder = f"{collection.FILES_DIR}/{elena.id}/"
    assert all(a.file.startswith(folder) for a in again.tavern.assets if a.held)
    assert all(pictures.read(file) is None for file in flat), "the old files went"
    farm = next(a for a in again.tavern.assets if a.name == "Farmhouse")
    assert pictures.read(farm.file) == FARMHOUSE
