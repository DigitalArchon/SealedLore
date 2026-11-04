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
    # Listed once, with how many versions.
    assert page.people.count() == 2 and "1 version" in page.people.item(1).text()
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
