"""Your characters in the window (gui/collection_page.py,
gui/window_collection.py): the page in the whole window and back, cards in
and out, every field kept, versions, deleting to the trash, the sheet of
their pictures, and pictures drawn for them landing on their cards."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox  # noqa: E402

import sealedlore.gui.window_collection as window_collection  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.image import GeneratedImage  # noqa: E402
from sealedlore.storage import collection  # noqa: E402
from sealedlore.storage.images import REFS_DIR  # noqa: E402
from sealedlore.storage.paths import COLLECTION_ID  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from sealedlore.storage.tavern_cards import read_card  # noqa: E402
from tests.test_tavern import ELENA, png_card, tiny_png, v2  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path):
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    yield window
    window.close()


def no_boxes(monkeypatch) -> list[str]:
    said: list[str] = []

    def exec_(box) -> int:
        said.append(f"{box.text()} {box.informativeText()}")
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    return said


def kept_elena(window: MainWindow, tmp_path: Path, monkeypatch):
    path = png_card(tmp_path, {"chara": v2()})
    monkeypatch.setattr(
        "sealedlore.gui.window_collection.QFileDialog.getOpenFileNames",
        lambda *args, **kwargs: ([str(path)], ""),
    )
    no_boxes(monkeypatch)
    window.show_collection(True)
    window.collection_page.action_requested.emit("import", "")
    return window.collection_page.current_character()


def test_the_page_takes_the_window_and_gives_the_story_back(app, window, story, cast):
    save_story_bundle(StoryBundle(story=story, cast=list(cast)), root=window.root)
    window.open_story(story.id)
    assert window.composer.isVisibleTo(window)

    window.collection_action.trigger()
    assert window.collection_shown()
    assert not window.composer.isVisibleTo(window)
    assert not window.inspector_dock.isVisibleTo(window)
    assert window.collection_page.back_button.text() == "Back to the story"

    window._on_escape()
    assert not window.collection_shown()
    assert window.centre_stack.currentWidget() is window.transcript
    assert window.composer.isVisibleTo(window) and window.inspector_dock.isVisibleTo(window)
    assert window.session.story.id == story.id


def test_with_no_story_it_closes_to_the_start_page(app, window):
    window.show_collection(True)
    assert window.collection_page.back_button.text() == "Close"
    # The start page asked for meanwhile doesn't take the window from it.
    window.reload_transcript()
    assert window.collection_shown()
    window.collection_page.back_button.click()
    assert window.centre_stack.currentWidget() is window.start_page


def test_cards_imported_on_the_page_are_kept_beside_the_stories(
    app, window, tmp_path: Path, monkeypatch
):
    elena = kept_elena(window, tmp_path, monkeypatch)
    assert elena is not None and elena.name == "Elena"
    assert window.collection_page.title.text() == "Elena"
    portrait = elena.reference_images[0]
    assert portrait.file.startswith(REFS_DIR)
    folder = window.root / "characters"
    assert (folder / portrait.file).is_file() and (folder / "cast.json").is_file()
    assert not (window.root / "stories").exists()
    # Every field of the card is on the page.
    page = window.collection_page
    assert page.card_form.personality.toPlainText() == ELENA["personality"]
    assert page.chat_form.first_mes.toPlainText() == ELENA["first_mes"]
    assert page.chat_form.greetings.list.count() == 2
    assert page.chat_form.system_prompt.toPlainText() == ELENA["system_prompt"]
    assert page.about_form.creator.text() == "someone"
    assert page.about_form.tags.text() == "survival"
    assert page.lorebook_form.list.count() == 1


def test_edits_on_the_page_are_kept(app, window, tmp_path: Path, monkeypatch):
    kept_elena(window, tmp_path, monkeypatch)
    page = window.collection_page
    page.card_form.personality.setPlainText("wary, loyal")
    page.chat_form.scenario.setPlainText("A farmhouse in the snow.")
    page.chat_form.greetings.list.setCurrentRow(0)
    page.chat_form.greetings.text.setPlainText("The door opens.")
    page.about_form.tags.setText("survival, farm")
    window._save_collection()

    again = collection.load_collection(window.root).cast[0]
    assert again.personality == "wary, loyal"
    assert again.tavern.scenario == "A farmhouse in the snow."
    assert again.tavern.alternate_greetings[0] == "The door opens."
    assert again.tavern.tags == ["survival", "farm"]
    assert page.tag.isVisibleTo(page) and page.tag.count() == 3


def test_a_new_character_and_a_search_for_one(app, window, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Marcus", True))
    window.show_collection(True)
    window.collection_page.new_button.click()
    page = window.collection_page
    assert page.current_character().name == "Marcus"
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Ben", True))
    page.new_button.click()
    page.card_form.full_description.setPlainText("Runs the store in Hellsville.")
    page.show_character(None)
    assert page.overview.count() == 2
    page.search.setText("hellsville store")
    assert page.overview.count() == 1
    names = [page.people.item(row).text().split("\n")[0] for row in range(page.people.count())]
    assert names == ["Everyone", "Ben"]


def test_a_version_is_its_own_card_and_outlives_its_main(app, window, tmp_path: Path, monkeypatch):
    elena = kept_elena(window, tmp_path, monkeypatch)
    page = window.collection_page
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("After the war", True))
    page.action_requested.emit("version", elena.id)
    version = page.current_character()
    assert version is not elena and version.collection.family == elena.id
    assert page.version.isVisibleTo(page)
    assert [page.version.itemText(i) for i in range(page.version.count())] == [
        "Main card",
        "After the war",
    ]
    # Listed once, with how many versions (the main card counts as one).
    assert page.people.count() == 2 and "2 versions" in page.people.item(1).text()
    version.personality = "harder now"
    assert elena.personality == ELENA["personality"]
    assert version.reference_images[0].file != elena.reference_images[0].file

    trashed: list[Path] = []
    monkeypatch.setattr(
        window_collection, "move_to_trash", lambda path: trashed.append(path) or True
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    page.show_character(elena.id)
    page.action_requested.emit("delete", elena.id)
    assert trashed and trashed[0].suffix == ".charx"
    kept = collection.load_collection(window.root).cast
    assert [c.id for c in kept] == [version.id]
    assert kept[0].collection.family == "" and kept[0].collection.version == ""
    # Her picture went with her; the version's own stays.
    assert not (window.root / "characters" / elena.reference_images[0].file).exists()
    assert (window.root / "characters" / version.reference_images[0].file).exists()


def test_a_kept_card_goes_out_with_every_field(app, window, tmp_path: Path, monkeypatch):
    elena = kept_elena(window, tmp_path, monkeypatch)
    out = tmp_path / "out.png"
    monkeypatch.setattr(
        "sealedlore.gui.window_collection.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(out), ""),
    )
    window.collection_page.action_requested.emit("export_png", elena.id)
    card = read_card(out)
    for key in ("first_mes", "scenario", "mes_example", "system_prompt", "personality", "tags"):
        assert card.fields[key] == ELENA[key], key
    assert card.fields["creator_notes"] == ELENA["creator_notes"]

    out = tmp_path / "out.charx"
    monkeypatch.setattr(
        "sealedlore.gui.window_collection.QFileDialog.getSaveFileName",
        lambda *args, **kwargs: (str(out), ""),
    )
    window.collection_page.action_requested.emit("export_charx", elena.id)
    assert read_card(out).picture is not None


def test_their_sheet_shows_their_pictures(app, window, tmp_path: Path, monkeypatch):
    elena = kept_elena(window, tmp_path, monkeypatch)
    page = window.collection_page
    page.show_tab("Pictures")
    board = page.board
    assert board.current == elena.id and board._sheet is not None
    assert board.people.isHidden() and board.back_button.isHidden()
    # Pictures from a file land on the place they were dropped on.
    picture = tmp_path / "angry.png"
    picture.write_bytes(tiny_png())
    board.files_dropped.emit(elena.id, "winter coat", "angry", [str(picture)])
    added = elena.reference_images[-1]
    assert (added.look, added.expression) == ("winter coat", "angry")
    kept = collection.load_collection(window.root).cast[0]
    assert [r.expression for r in kept.reference_images][-1] == "angry"


def test_pictures_drawn_for_them_land_on_their_cards(app, window, tmp_path: Path, monkeypatch):
    elena = kept_elena(window, tmp_path, monkeypatch)
    session = window._collection_session()
    # The fill dialog's approved request goes to the image jobs with the
    # characters' own session, under their folder's id.
    started: list = []
    monkeypatch.setattr(window, "_card_pictures_refused", lambda session: False)
    monkeypatch.setattr(
        window,
        "_start_card_pictures",
        lambda requests, session=None: started.append((requests, session)),
    )
    monkeypatch.setattr(
        "sealedlore.gui.window_collection.ask_fill_gaps", lambda *a, **k: (["request"], [])
    )
    window.collection_page.board.fill_requested.emit(elena.id, [])
    assert started == [(["request"], session)]

    # A picture drawn for her card comes in: onto her card, with its labels.
    session.pictures.write("images/drawn.png", tiny_png())
    record = GeneratedImage(
        file="images/drawn.png",
        prompt="Make her angry.",
        model="m",
        size="1024x1024",
        card_id=elena.id,
        card_look="winter coat",
        card_expression="angry",
    )
    window._on_image_finished("job", COLLECTION_ID, [record], session.pictures)
    landed = elena.reference_images[-1]
    assert (landed.look, landed.expression, landed.drawn_as) == ("winter coat", "angry", record.id)
    kept = collection.load_collection(window.root).cast[0]
    assert kept.reference_images[-1].expression == "angry"
    # Not the story's: no story is open, none was made.
    assert window.session is None


def test_drawing_for_them_isnt_held_by_the_storys_job(app, window, story, cast, monkeypatch):
    save_story_bundle(StoryBundle(story=story, cast=list(cast)), root=window.root)
    window.open_story(story.id)
    window._busy = True
    monkeypatch.setattr(type(window.config), "images", lambda self: SimpleNamespace())
    assert window._card_pictures_refused(window.session)
    assert not window._card_pictures_refused(window._collection_session())
    window._busy = False


def test_their_folder_holds_no_story_and_stories_list_none_of_them(app, window, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Marcus", True))
    window.show_collection(True)
    window.collection_page.new_button.click()
    window.stories.refresh()
    assert window.stories.list.count() == 0
    marcus = collection.load_collection(window.root).cast[0]
    assert marcus.name == "Marcus" and marcus.collection is not None


# --- what the review found (Nov 4 2026) ---------------------------------------------------


def test_the_window_starts_whatever_is_in_their_folder(app, tmp_path: Path, monkeypatch):
    root = tmp_path / "data"
    folder = root / "characters"
    folder.mkdir(parents=True)
    (folder / "story.json").write_text('{"id": "your-characters", "title": "Your characters"}')
    (folder / "cast.json").write_text('[{"name": ')  # cut off
    window = MainWindow(root=root, use_mock=True)  # never read at start
    assert window._collection is None
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(text))
    window.show_collection(True)
    assert "couldn't be read" in window.collection_page.stats.text()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("Marcus", True))
    window.collection_page.new_button.click()
    assert warned and (folder / "cast.json").read_text() == '[{"name": '  # never written over
    window.close()


def test_what_the_story_does_meanwhile_stays_under_the_page(app, window, story, cast):
    save_story_bundle(StoryBundle(story=story, cast=list(cast)), root=window.root)
    window.open_story(story.id)
    window.show_collection(True)
    # A job ending meanwhile refreshes the bars and banners: not over the page.
    window.refresh_branch_bar()
    window._sync_place_button()
    window.refresh_summaries()
    for widget in (window.branch_bar, window.place_button, window.staleness, window.over_budget):
        assert widget.isHidden()
    # Ctrl+Enter in a card's field never sends the story's unsent turn.
    window.composer.input.setPlainText("Not yet.")
    window.send_turn()
    assert window.composer.input.toPlainText() == "Not yet." and not window._busy
    window.open_find()
    assert not window.find_open()
    window.show_collection(False)
    assert not window.composer.isHidden()


def test_a_memory_chat_opened_from_the_page_closes_it(app, window):
    from sealedlore.models.story import Story

    window.show_collection(True)
    window.open_memory_chat(
        StoryBundle(story=Story(title="Quiet", mode="chat", chat_keep="memory"))
    )
    assert not window.collection_shown() and not window.composer.isHidden()
    window._private_allows_close = lambda: True


def test_the_page_shows_a_card_as_it_is_after_a_story_changed_it(
    app, window, tmp_path: Path, monkeypatch
):
    elena = kept_elena(window, tmp_path, monkeypatch)
    window.show_collection(False)
    elena.personality = "changed elsewhere"  # as a story's Save to the card does
    window.show_collection(True, elena.id)
    assert window.collection_page.card_form.personality.toPlainText() == "changed elsewhere"


def test_a_story_job_running_says_why_the_page_waits(app, window, tmp_path: Path, monkeypatch):
    elena = kept_elena(window, tmp_path, monkeypatch)
    window._busy = True
    window.chat_with_kept(elena.id)
    assert "current job" in window.statusBar().currentMessage()
    window._busy = False


# --- what the review of 3.0.0b2 found (Nov 5 2026) -----------------------------------------


def coloured_png(colour: str) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(QColor(colour))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def test_an_unreadable_folder_gets_no_pictures_and_no_link(app, tmp_path: Path, monkeypatch):
    """Saves were held back, but pictures went in all the same, and a story's
    character was linked to a card never kept."""
    from sealedlore.models.character import Character
    from sealedlore.models.story import Story

    root = tmp_path / "data"
    folder = root / "characters"
    folder.mkdir(parents=True)
    (folder / "story.json").write_text('{"id": "your-characters", "title": "Your characters"}')
    (folder / "cast.json").write_text('[{"name": ')  # cut off
    story = Story(id="story-1", title="Doomsville", held_character_id="john")
    save_story_bundle(StoryBundle(story=story, cast=[Character(id="john", name="John")]), root=root)
    window = MainWindow(root=root, use_mock=True)
    window.open_story("story-1")
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(title))
    window.keep_in_collection(window.session.cast[0])
    assert warned == ["Not kept"] and window.session.cast[0].linked is None
    window._import_to_collection([str(png_card(tmp_path, {"chara": v2()}))])
    assert warned == ["Not kept", "Not kept"]
    assert not (folder / "images").exists(), "no picture written into it"
    assert (folder / "cast.json").read_text() == '[{"name": '
    window._private_allows_close = lambda: True
    window.close()


def test_a_cancelled_import_makes_no_folder(app, window, monkeypatch):
    monkeypatch.setattr(
        "sealedlore.gui.window_collection.QFileDialog.getOpenFileNames",
        lambda *args, **kwargs: ([], ""),
    )
    window._import_to_collection()
    assert not (window.root / "characters").exists()


def test_a_picture_taken_off_before_any_other_change_leaves_the_disk(
    app, window, tmp_path: Path, monkeypatch
):
    """Only files named at the last look were removed: one that came in
    since (an import, a picture landing) and went again stayed for good."""
    elena = kept_elena(window, tmp_path, monkeypatch)
    portrait = window.root / "characters" / elena.reference_images[0].file
    assert portrait.exists()
    elena.reference_images.clear()
    window._save_collection()
    assert not portrait.exists()


def test_a_deleted_cards_files_stay_while_the_folder_still_names_them(
    app, window, tmp_path: Path, monkeypatch
):
    elena = kept_elena(window, tmp_path, monkeypatch)
    portrait = window.root / "characters" / elena.reference_images[0].file
    monkeypatch.setattr(window_collection, "move_to_trash", lambda path: True)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(window, "_warn_plain", lambda *a: None)

    def full(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(window._collection, "save", full)
    window.collection_page.action_requested.emit("delete", elena.id)
    assert portrait.exists(), "the save failed: cast.json on disk still names it"


def test_a_file_under_a_name_used_before_shows_its_own_picture(
    app, window, tmp_path: Path, monkeypatch
):
    elena = kept_elena(window, tmp_path, monkeypatch)
    red, blue = tmp_path / "red" / "bg.png", tmp_path / "blue" / "bg.png"
    for path, colour in ((red, "red"), (blue, "blue")):
        path.parent.mkdir()
        path.write_bytes(coloured_png(colour))
    window._add_card_files(elena, [str(red)])
    asset = elena.tavern.assets[-1]
    thumbs = window.collection_page._thumbs
    assert thumbs.now(asset.file, 8, 8).toImage().pixelColor(4, 4).name() == "#ff0000"
    window._remove_card_file = lambda card, a: None  # (asks first; taken off by hand here)
    elena.tavern.assets.remove(asset)
    window._collection_store().remove(asset.file)
    window._add_card_files(elena, [str(blue)])
    again = elena.tavern.assets[-1]
    assert again.file == asset.file, "the same name, as the card gave it"
    assert thumbs.now(again.file, 8, 8).toImage().pixelColor(4, 4).name() == "#0000ff"


def test_typing_in_the_makers_notes_doesnt_refill_the_list(
    app, window, tmp_path: Path, monkeypatch
):
    kept_elena(window, tmp_path, monkeypatch)
    page = window.collection_page
    fills: list[int] = []
    monkeypatch.setattr(page, "_fill", lambda: fills.append(1))
    form = page.about_form
    notes = next(
        box for name, box in vars(form).items() if "note" in name and hasattr(box, "setPlainText")
    )
    notes.setPlainText("Written for a long winter.")
    assert fills == [], "the list shows nothing the notes change"


def test_a_story_from_their_cards_scenario_asks_to_leave_before_it_is_saved(
    app, window, tmp_path: Path, monkeypatch
):
    from PySide6.QtWidgets import QDialog

    from sealedlore.gui.tavern_dialog import CardImportDialog

    elena = kept_elena(window, tmp_path, monkeypatch)
    monkeypatch.setattr(CardImportDialog, "exec", lambda dialog: QDialog.Accepted)
    monkeypatch.setattr(window, "_leave_for_new", lambda: False)  # Cancel: stay
    monkeypatch.setattr(window, "open_setup", lambda: False)
    window._story_from_kept(elena.id)
    window.stories.refresh()
    assert window.stories.list.count() == 0, "no story left behind"


def gif_with_comment(secret: bytes) -> bytes:
    """A 1×1 GIF carrying a comment: hidden data only a redraw removes."""
    header = b"GIF89a" + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + bytes([0x80, 0, 0])
    palette = b"\x00\x00\x00\xff\xff\xff"
    comment = b"\x21\xfe" + bytes([len(secret)]) + secret + b"\x00"
    image = b"\x2c" + bytes(4) + (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + b"\x00"
    return header + palette + comment + image + b"\x02\x02\x44\x01\x00" + b"\x3b"


def heic_with_exif(secret: bytes) -> bytes:
    """An iPhone-style HEIC Qt can't read, its EXIF inside."""
    import struct

    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", 8 + len(payload)) + kind + payload

    handler = box(b"hdlr", b"\x00" * 8 + b"pict" + b"\x00" * 13)
    return (
        box(b"ftyp", b"heic\x00\x00\x00\x00mif1heic")
        + box(b"meta", b"\x00" * 4 + handler)
        + box(b"mdat", b"Exif\x00\x00MM\x00*" + secret)
    )


def test_a_picture_of_any_kind_added_on_the_files_tab_keeps_no_hidden_data(
    app, window, tmp_path: Path, monkeypatch
):
    """Only JPEG, PNG and WebP were cleaned: a GIF's comment, a HEIC's GPS
    went onto the card and out with it in an export."""
    elena = kept_elena(window, tmp_path, monkeypatch)
    said: list = []
    monkeypatch.setattr(window, "_say_left_out", lambda *args: said.append(args))
    gif, heic = tmp_path / "wave.gif", tmp_path / "photo.heic"
    gif.write_bytes(gif_with_comment(b"GPS 48.8584 N home"))
    heic.write_bytes(heic_with_exif(b"GPS 48.8584 N home"))
    window._add_card_files(elena, [str(gif), str(heic)])
    store = window._collection_store()
    kept = [store.read(asset.file) for asset in elena.tavern.assets if asset.held]
    assert len(kept) == 1 and b"48.8584" not in kept[0], "the GIF redrawn, its comment gone"
    assert said and any("photo.heic" in note for note in said[0][2])


def test_a_picture_nothing_can_clean_is_left_out_of_what_comes_in(app):
    from sealedlore.gui.ref_images import clean_incoming

    files = {"images/refs/a.heic": heic_with_exif(b"home"), "story.json": b"{}"}
    cleaned = clean_incoming(files)
    assert "images/refs/a.heic" not in cleaned and "story.json" in cleaned


def test_the_trash_takes_a_card_whose_picture_an_export_would_refuse(
    app, window, tmp_path: Path, monkeypatch
):
    """A picture kept from 3.0.0b2 that can't be stripped stops an export
    (it leaves this computer); Delete's copy to the Trash takes it as it is."""
    from sealedlore.storage.tavern_cards import CardError

    elena = kept_elena(window, tmp_path, monkeypatch)
    store = window._collection_store()
    store.write(elena.reference_images[0].file, heic_with_exif(b"home"))
    with pytest.raises(CardError):
        window._charx_of(elena)
    assert window._charx_of(elena, shared=False)
