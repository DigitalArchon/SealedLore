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

from PySide6.QtCore import Qt  # noqa: E402
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


def test_the_lists_buttons_are_whole_in_its_column(app, window):
    """The list's column is a fixed width: New character, Import and Backup
    in one row cut "New character" short ("w charac"). Each is whole, inside
    the column, wrapping to a second line where they don't fit."""
    from sealedlore.gui.app import load_stylesheet
    from tests.test_ui_tidy import needs_real_fonts, settle

    needs_real_fonts()
    before = app.styleSheet()
    # The buttons' padding is the stylesheet's: unstyled, all three fitted.
    app.setStyleSheet(load_stylesheet())
    try:
        window.resize(1920, 1080)
        window.show()
        window.show_collection(True)
        settle(app)
        page = window.collection_page
        for button in (page.new_button, page.import_button, page.backup_button):
            assert button.isVisible(), button.text()
            assert button.width() >= button.sizeHint().width(), button.text()
            assert button.geometry().right() < page._left.width(), button.text()
    finally:
        app.setStyleSheet(before)


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
    assert (
        warned and (folder / "cast.json").read_text(encoding="utf-8") == '[{"name": '
    )  # never written over
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
    window._private_allows_close = window._private_may_close = lambda: True


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
    assert (folder / "cast.json").read_text(encoding="utf-8") == '[{"name": '
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


# --- a backup of all of them ---------------------------------------------------------------


def test_all_your_characters_backed_up_come_back_on_another_computer(
    app, window, tmp_path: Path, monkeypatch
):
    """One card file each lost their versions and their ids (a story's links
    to them); the backup keeps both, and File → Import… puts it back."""
    from PySide6.QtWidgets import QFileDialog

    elena = kept_elena(window, tmp_path, monkeypatch)
    page = window.collection_page
    backup = tmp_path / "all.sealedlore-characters.zip"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(backup), ""))
    page.backup_action.trigger()
    assert backup.exists() and "Backed up 1 character" in window.statusBar().currentMessage()
    portrait = window._collection_store().read(elena.reference_images[0].file)

    other = MainWindow(root=tmp_path / "elsewhere", use_mock=True)
    try:
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(backup), ""))
        other.import_file()
        kept = other._kept()
        assert [c.id for c in kept] == [elena.id], "her id, so a story's link to her holds"
        assert other._collection_store().read(kept[0].reference_images[0].file) == portrait
        assert other.collection_shown()
    finally:
        other.close()


def test_restoring_characters_you_have_asks_and_keeps_yours_by_default(
    app, window, tmp_path: Path, monkeypatch
):
    from PySide6.QtWidgets import QFileDialog

    elena = kept_elena(window, tmp_path, monkeypatch)
    backup = tmp_path / "all.sealedlore-characters.zip"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(backup), ""))
    window.collection_page.backup_action.trigger()
    elena.full_description = "Changed since the backup."
    asked: list = []

    def exec_(box) -> int:
        asked.append((box.windowTitle(), box.defaultButton().text()))
        return 0  # Enter: the default

    monkeypatch.setattr(QMessageBox, "exec", exec_)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda box: box.defaultButton())
    window.restore_collection(backup)
    assert asked == [("Restore your characters", "Keep mine")]
    assert window._kept_card(elena.id).full_description == "Changed since the backup."


# --- several at once -----------------------------------------------------------------------


def three_kept(window, tmp_path: Path, monkeypatch):
    """Elena (from her card), Ben, and Ben's version: the list shows two."""
    from sealedlore.models.collection import CollectionInfo

    elena = kept_elena(window, tmp_path, monkeypatch)
    ben = collection.kept("Ben")
    older = collection.kept("Ben")
    older.collection = CollectionInfo(family=ben.id, version="Older")
    window._collection_session().cast.extend([ben, older])
    window._save_collection()
    window._refresh_collection()
    return elena, ben, older


def choose(page, *ids: str) -> None:
    page.people.clearSelection()
    for row in range(page.people.count()):
        item = page.people.item(row)
        if item.data(Qt.UserRole) in ids:
            item.setSelected(True)


def test_two_chosen_in_the_list_show_what_can_be_done_to_both(
    app, window, tmp_path: Path, monkeypatch
):
    elena, ben, _older = three_kept(window, tmp_path, monkeypatch)
    page = window.collection_page
    choose(page, elena.id, ben.id)
    assert page.pages.currentIndex() == 2 and page.title.text() == "2 characters"
    assert "Elena" in page.chosen_names.text() and "Ben" in page.chosen_names.text()
    choose(page, ben.id)
    page.show_character(ben.id)
    assert page.pages.currentIndex() == 1, "one chosen: their card"


def test_a_tag_goes_on_and_off_every_one_chosen_and_their_versions(
    app, window, tmp_path: Path, monkeypatch
):
    elena, ben, older = three_kept(window, tmp_path, monkeypatch)
    page = window.collection_page
    choose(page, elena.id, ben.id)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("  winter  road ", True))
    page._chosen("tag")
    for card in (elena, ben, older):
        assert collection.has_tag(window._kept_card(card.id), "winter road")
    saved = {c.id: c for c in collection.load_collection(window.root).cast}
    assert collection.has_tag(saved[older.id], "winter road")
    choose(page, elena.id, ben.id)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("winter road", True))
    page._chosen("untag")
    assert not any(
        collection.has_tag(window._kept_card(c.id), "winter road") for c in (elena, ben, older)
    )


def test_those_chosen_stay_chosen_after_a_tag_while_two_are_listed(
    app, window, tmp_path: Path, monkeypatch
):
    """A tag added to them refreshed the list and let go of them: the next
    thing done to them, an export say, found nobody chosen (the lab walk)."""
    elena, ben, _older = three_kept(window, tmp_path, monkeypatch)
    page = window.collection_page
    choose(page, elena.id, ben.id)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("winter", True))
    page._chosen("tag")
    assert sorted(page.chosen_ids()) == sorted([elena.id, ben.id])
    assert page.pages.currentIndex() == 2 and page.title.text() == "2 characters"
    page.search.setText("Ben")
    assert len(page.chosen_ids()) < 2 and page.pages.currentIndex() != 2, "one left listed"
    page.search.setText("")
    assert page.pages.currentIndex() != 2


def test_those_chosen_are_exported_into_a_folder_versions_too(
    app, window, tmp_path: Path, monkeypatch
):
    from PySide6.QtWidgets import QFileDialog

    elena, ben, _older = three_kept(window, tmp_path, monkeypatch)
    folder = tmp_path / "out"
    folder.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))
    choose(window.collection_page, elena.id, ben.id)
    window.collection_page._chosen("export_charx")
    assert sorted(p.name for p in folder.iterdir()) == [
        "Ben - Older.charx",
        "Ben.charx",
        "Elena.charx",
    ]


def test_those_chosen_are_deleted_once_asked_and_cancel_is_the_default(
    app, window, tmp_path: Path, monkeypatch
):
    elena, ben, older = three_kept(window, tmp_path, monkeypatch)
    portrait = window.root / "characters" / elena.reference_images[0].file
    trashed: list = []
    monkeypatch.setattr(
        window_collection, "move_to_trash", lambda path: trashed.append(path.name) or True
    )
    asked: list = []

    def question(_parent, title, _text, _buttons, default):
        asked.append((title, default))
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", question)
    choose(window.collection_page, elena.id, ben.id)
    window.collection_page._delete_key()  # the Delete key, with two chosen
    assert asked == [("Delete characters", QMessageBox.Cancel)]
    assert window._kept() == [] and len(trashed) == 3
    assert not portrait.exists()
    assert collection.load_collection(window.root).cast == []


def test_those_chosen_join_the_story_ticked_in_the_picker(
    app, window, tmp_path: Path, monkeypatch, story, cast
):
    from PySide6.QtWidgets import QDialog

    from sealedlore.gui.collection_picker import CollectionPicker

    save_story_bundle(StoryBundle(story=story, cast=list(cast)), root=window.root)
    window.open_story(story.id)
    elena, ben, _older = three_kept(window, tmp_path, monkeypatch)
    window.show_collection(True)
    ticked: list = []

    def exec_(picker) -> int:
        ticked.extend(picker.chosen())
        return QDialog.Rejected

    monkeypatch.setattr(CollectionPicker, "exec", exec_)
    choose(window.collection_page, elena.id, ben.id)
    assert window.collection_page.chosen_add.isVisibleTo(window.collection_page)
    window.collection_page._chosen("add_to_story")
    assert sorted(card.id for card in ticked) == sorted([elena.id, ben.id])


def test_a_cards_file_is_replaced_keeping_its_name_kind_and_place(
    app, window, tmp_path: Path, monkeypatch
):
    """A greeting shows the file by its name: replaced, the name and the card's
    place for it stay, so it still does."""
    from PySide6.QtWidgets import QFileDialog

    elena = kept_elena(window, tmp_path, monkeypatch)
    red, blue = tmp_path / "barn.png", tmp_path / "barn-blue.png"
    red.write_bytes(coloured_png("red"))
    blue.write_bytes(coloured_png("blue"))
    window._add_card_files(elena, [str(red)])
    index = len(elena.tavern.assets) - 1
    asset = elena.tavern.assets[index]
    asset.path, before = "assets/other/barn.png", asset.file
    store = window._collection_store()

    def replace_with(path: Path) -> None:
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
        window._collection_file("replace", index, elena.id)

    replace_with(blue)
    assert asset.file == before and asset.name == "barn" and asset.path == "assets/other/barn.png"
    assert store.read(asset.file) == coloured_png("blue")
    jpeg = tmp_path / "barn.jpg"
    from PySide6.QtGui import QColor, QImage

    image = QImage(8, 8, QImage.Format_RGB32)
    image.fill(QColor("green"))
    image.save(str(jpeg), "JPEG")
    replace_with(jpeg)
    assert asset.ext == "jpg" and asset.file.endswith(".jpg") and asset.name == "barn"
    assert store.read(before) is None, "the old file went once the record was saved"
    warned: list[str] = []
    monkeypatch.setattr(window, "_warn_plain", lambda title, text: warned.append(title))
    heic = tmp_path / "photo.heic"
    heic.write_bytes(heic_with_exif(b"home"))
    kept = asset.file
    replace_with(heic)
    assert warned == ["Not replaced"] and asset.file == kept
