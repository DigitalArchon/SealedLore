"""The cast board (gui/cast_board.py, gui/window_board.py): every
character's pictures in the whole window, each as a sheet of looks and
expressions with its gaps ready to draw. Offscreen; nothing is sent to an
image model."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QItemSelectionModel  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sealedlore.gui.fill_gaps import FillGapsDialog  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.storage.repository import (  # noqa: E402
    StoryBundle,
    load_story_bundle,
    save_story_bundle,
)
from tests.conftest import make_exchange  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    session = window.session
    looks = [
        ("p", "normal clothes", "neutral"),
        ("h", "normal clothes", "happy"),
        ("a", "combat armour", "neutral"),
        ("u", "", ""),
    ]
    refs = []
    for ref_id, look, expression in looks:
        session.pictures.write(f"images/refs/{ref_id}.png", b"picture")
        refs.append(
            ImageRef(id=ref_id, file=f"images/refs/{ref_id}.png", look=look, expression=expression)
        )
    serrik(window).reference_images = refs
    session.full_path()[-1].meta.looks_wanted = {"char-serrik": "combat armour · angry"}
    yield window
    window.close()


def serrik(window: MainWindow):
    return next(c for c in window.session.cast if c.id == "char-serrik")


def cell_of(window: MainWindow, look: str, expression: str):
    board = window.cast_board
    sheet = board._sheet
    return board.sheet.cell_at(sheet.looks.index(look), sheet.expressions.index(expression))


def test_the_board_takes_the_window_and_gives_it_back_as_it_was(app, window):
    window.show()
    window.config.stories_list = False
    window._sync_stories_list()
    window.inspector_dock.setVisible(True)
    branch_bar = window.branch_bar.isVisible()
    window.cast_panel.board_button.click()
    assert window.board_shown()
    for widget in (window.inspector_dock, window.stories_dock, window.composer, window.find_button):
        assert not widget.isVisible()
    window._on_escape()
    assert not window.board_shown()
    assert window.inspector_dock.isVisible() and window.composer.isVisible()
    assert not window.stories_dock.isVisible()  # put away before, put away after
    assert window.branch_bar.isVisible() == branch_bar
    assert window.centre_stack.currentWidget() is window.transcript


def test_a_characters_sheet_shows_their_pictures_and_their_gaps(app, window):
    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    assert board.current == "char-serrik"
    assert board._sheet.looks == ["normal clothes", "combat armour"]
    assert board.sheet.rowCount() == 2 and board.sheet.columnCount() == 5
    shown = cell_of(window, "normal clothes", "neutral")
    assert shown.ref.id == "p" and shown.portrait_id == "p"
    gap = cell_of(window, "combat armour", "angry")
    assert gap.refs == [] and gap.gap.source.id == "a" and gap.gap.wanted == 1
    # Unlabelled pictures wait in the tray, to be labelled.
    assert board.tray.count() == 1 and board.tray.isVisibleTo(board)
    assert board.fill_button.text() == "Fill every gap (7)…"
    # Choosing gaps: the button says how many.
    sheet = board._sheet
    index = board.sheet.model().index(1, sheet.expressions.index("angry"))
    board.sheet.selectionModel().select(index, QItemSelectionModel.Select)
    assert board.fill_button.text() == "Draw 1 chosen…"


def test_filling_gaps_asks_for_only_what_differs_from_their_picture(app, window, monkeypatch):
    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    gaps = [board._sheet.gap("combat armour", "angry"), board._sheet.gap("normal clothes", "sad")]
    monkeypatch.setattr(window.image_catalog, "ensure", lambda *args, **kwargs: None)
    dialog = FillGapsDialog(window.session, serrik(window), gaps, catalog=None, browse=None)
    requests = dialog.requests()
    assert [(r.card_look, r.card_expression) for r in requests] == [
        ("combat armour", "angry"),
        ("normal clothes", "sad"),
    ]
    assert [r.references[0].ref_id for r in requests] == ["a", "p"]
    assert all(not r.card_first for r in requests)
    assert "Change their expression or pose to: angry." in requests[0].prompt
    assert "clothes" not in requests[0].prompt.split("Keep")[0]
    # Untick one and change a prompt: what is approved is what is sent.
    dialog.ticks[1].setChecked(False)
    dialog.prompts[0].setPlainText("Make him furious.")
    assert [r.prompt for r in dialog.requests()] == ["Make him furious."]
    assert dialog.draw_button.text() == "Draw 1"
    dialog.deleteLater()

    # From the board: the dialog's requests are started as card pictures.
    started: list = []
    monkeypatch.setattr(window, "_card_pictures_refused", lambda session: False)
    monkeypatch.setattr(window, "_start_card_pictures", started.extend)
    monkeypatch.setattr(
        "sealedlore.gui.window_board.ask_fill_gaps", lambda *a, **k: (["request"], [])
    )
    board.fill_requested.emit("char-serrik", gaps)
    assert started == ["request"]


def test_cells_being_drawn_say_so(app, window, monkeypatch):
    from types import SimpleNamespace

    request = SimpleNamespace(
        card_id="char-serrik", card_first=False, card_look="combat armour", card_expression="Angry"
    )
    monkeypatch.setattr(
        window.image_jobs, "pending", lambda story_id=None: [SimpleNamespace(request=request)]
    )
    window.show_cast_board(True, "char-serrik")
    assert cell_of(window, "combat armour", "angry").drawing == 1
    assert cell_of(window, "combat armour", "happy").drawing == 0
    assert window.cast_board.fill_button.text() == "Fill every gap (6)…"
    # Closing with pictures being drawn asks: none are, really.
    monkeypatch.undo()


def test_looks_and_expressions_of_ones_own_are_kept_on_the_card(app, window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("formal clothes", True))
    board._add_look()
    assert serrik(window).sheet_looks == ["formal clothes"]
    assert board._sheet.looks[-1] == "formal clothes"
    assert board._sheet.gap("formal clothes", "neutral").source is not None
    assert board._sheet.gap("formal clothes", "happy").source is None

    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("laughing", True))
    board._add_expression(everyone=True)
    for character in window.session.cast:
        assert "laughing" in character.sheet_expressions
    saved = load_story_bundle(window.session.story.id, root=window.root)
    card = next(c for c in saved.cast if c.id == "char-serrik")
    assert card.sheet_looks == ["formal clothes"] and "laughing" in card.sheet_expressions

    # A column renamed: every picture in it is relabelled.
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("smiling", True))
    board._rename(serrik(window), expression="happy")
    assert next(r for r in serrik(window).reference_images if r.id == "h").expression == "smiling"


def test_a_pictures_own_actions_from_the_sheet(app, window, monkeypatch):
    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    board.picture_action.emit("portrait", "char-serrik", "a")
    assert serrik(window).reference_images[0].id == "a"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    board.picture_action.emit("remove", "char-serrik", "h")
    assert "h" not in [r.id for r in serrik(window).reference_images]
    shown: list = []
    monkeypatch.setattr(window, "_show_look", lambda card_id, ref_id: shown.append(ref_id))
    board.picture_action.emit("show", "char-serrik", "a")
    assert shown == ["a"]
    # While the story's job runs, the cards are looked at, not changed.
    window._busy = True
    window._refresh_board()
    assert not board.add_look_button.isEnabled()
    board.picture_action.emit("portrait", "char-serrik", "p")
    assert serrik(window).reference_images[0].id == "a"
    window._busy = False


def test_everyone_is_on_the_overview_and_a_chat_has_no_board(app, window):
    window.show_cast_board(True)
    board = window.cast_board
    assert board.current is None and board.pages.currentIndex() == 0
    names = [board.people.item(i).text().split("\n")[0] for i in range(board.people.count())]
    assert names[0] == "Everyone"
    assert set(names[1:]) == {c.name for c in window.session.cast}
    assert len(board._overview_lists) == len(window.session.cast)
    board._overview_lists[0].itemClicked.emit(board._overview_lists[0].item(0))
    assert board.current == window.session.cast[0].id
    window.show_cast_board(False)
    window.session.story.mode = "chat"
    window._update_controls()
    assert not window.board_action.isVisible()
    window.show_cast_board(True)
    assert not window.board_shown()


def test_show_spoilers_on_the_board_shows_everyone(app, window, tmp_path):
    """The Plot page's setting, offered on the board too: the plot's hidden
    characters, marked as not in the story yet."""
    from sealedlore.models.plot import Plot

    window.session.story.plot = Plot()
    hidden = next(c for c in window.session.cast if c.id != "char-serrik")
    hidden.plot_hidden = True
    window.config.show_plot_spoilers = False
    window.show_cast_board(True)
    board = window.cast_board
    assert not board.spoilers.isHidden() and not board.spoilers.isChecked()
    names = [board.people.item(i).text() for i in range(board.people.count())]
    assert not any(name.startswith(hidden.name) for name in names)
    board.spoilers.setChecked(True)
    assert window.config.show_plot_spoilers
    names = [board.people.item(i).text() for i in range(board.people.count())]
    shown = next(name for name in names if name.startswith(hidden.name))
    assert shown.endswith("not in the story yet")
    board.spoilers.setChecked(False)
    assert not window.config.show_plot_spoilers


def test_a_new_looks_whole_row_is_approved_at_once_and_drawn_from_its_first(
    app, window, monkeypatch
):
    """The row's first picture is drawn first; the rest wait for it and are
    drawn from it with the prompts approved, so the row wears one outfit."""
    from sealedlore.gui.fill_gaps import FillGapsDialog

    card = serrik(window)
    card.sheet_looks = ["formal clothes"]
    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    row = board._sheet.looks.index("formal clothes")
    gaps = board._line_gaps(row=row)
    assert [g.expression for g in gaps] == ["neutral", "happy", "sad", "angry", "afraid"]
    dialog = FillGapsDialog(window.session, card, gaps, catalog=None, browse=None)
    assert [r.card_expression for r in dialog.requests()] == ["neutral"]
    waiting = dialog.waiting()
    assert [w.expression for w in waiting] == ["happy", "sad", "angry", "afraid"]
    assert waiting[0].prompt.startswith(
        "Edit this picture: Change their expression or pose to: happy."
    )
    # Unticking the first lets the rest go with it.
    dialog.ticks[0].setChecked(False)
    assert dialog.waiting() == [] and not dialog.ticks[1].isEnabled()
    dialog.ticks[0].setChecked(True)
    assert dialog.ticks[1].isEnabled()
    dialog.deleteLater()

    started: list = []
    monkeypatch.setattr(window, "_card_pictures_refused", lambda session: False)
    monkeypatch.setattr(window, "_start_card_pictures", started.extend)
    monkeypatch.setattr(
        "sealedlore.gui.window_board.ask_fill_gaps", lambda *a, **k: (["first"], waiting)
    )
    board.fill_requested.emit("char-serrik", gaps)
    assert started == ["first"]
    assert cell_of(window, "formal clothes", "happy").drawing == 1
    # Its first picture lands: the rest are drawn from it.
    window.session.pictures.write("images/refs/f.png", b"picture")
    card.reference_images.append(
        ImageRef(id="f", file="images/refs/f.png", look="formal clothes", expression="neutral")
    )
    window._start_waiting()
    assert [(r.card_look, r.card_expression) for r in started[1:]] == [
        ("formal clothes", e) for e in ("happy", "sad", "angry", "afraid")
    ]
    assert all(r.references[0].ref_id == "f" for r in started[1:])
    assert window._board_waiting == []


def test_a_new_looks_row_is_let_go_when_its_first_picture_fails(app, window, monkeypatch):
    from sealedlore.gui.fill_gaps import Waiting

    serrik(window).sheet_looks = ["formal clothes"]
    window._board_waiting = [
        (
            window.session.story.id,
            Waiting("char-serrik", "formal clothes", "happy", "Happy.", "model"),
        )
    ]
    window._start_waiting()  # nothing being drawn for that look: let go
    assert window._board_waiting == []
    assert "didn't come" in window.statusBar().currentMessage()


def test_pictures_dropped_on_a_place_take_its_labels(app, window, monkeypatch):
    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    sheet = board._sheet
    # A picture of theirs dragged to another place: relabelled.
    board.sheet.picture_dropped.emit(
        "h", sheet.looks.index("combat armour"), sheet.expressions.index("angry")
    )
    ref = next(r for r in serrik(window).reference_images if r.id == "h")
    assert (ref.look, ref.expression) == ("combat armour", "angry")
    # Onto the tray: its labels taken off.
    board.tray.picture_dropped.emit("h")
    assert (ref.look, ref.expression) == ("", "")
    # The tray's own dragged onto a place: labelled.
    board.sheet.picture_dropped.emit("u", 0, sheet.expressions.index("sad"))
    unlabelled = next(r for r in serrik(window).reference_images if r.id == "u")
    assert (unlabelled.look, unlabelled.expression) == ("normal clothes", "sad")

    # Files from the desktop dropped on a place: added there, labelled.
    from sealedlore.gui import crop_dialog

    monkeypatch.setattr(
        crop_dialog, "cropped_picture", lambda parent, path, shape, counter="": (b"pic", ".png")
    )
    before = len(serrik(window).reference_images)
    board.sheet.files_dropped.emit(["/somewhere/smile.png"], 0, sheet.expressions.index("afraid"))
    refs = serrik(window).reference_images
    assert len(refs) == before + 1
    assert (refs[-1].look, refs[-1].expression) == ("normal clothes", "afraid")


def test_the_sheet_answers_its_keys(app, window, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    seen: list = []
    monkeypatch.setattr(window, "_show_whole", lambda title, data: seen.append(("whole", title)))
    monkeypatch.setattr(
        "sealedlore.gui.window_board.edit_labels", lambda *a: seen.append(("labels",)) or False
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    board.sheet.setCurrentCell(0, 0)
    QTest.keyClick(board.sheet, Qt.Key_Return)
    QTest.keyClick(board.sheet, Qt.Key_F2)
    QTest.keyClick(board.sheet, Qt.Key_Delete)
    assert seen[0][0] == "whole" and seen[1] == ("labels",)
    assert len(serrik(window).reference_images) == 4  # asked, and answered No
    # Enter on a gap draws the gaps chosen.
    monkeypatch.setattr(window, "_card_pictures_refused", lambda session: True)
    asked: list = []
    board.fill_requested.connect(lambda card_id, gaps: asked.append(gaps))
    sheet = board._sheet
    board.sheet.setCurrentCell(1, sheet.expressions.index("sad"))
    QTest.keyClick(board.sheet, Qt.Key_Return)
    assert [(g.look, g.expression) for g in asked[-1]] == [("combat armour", "sad")]


def test_pictures_kept_from_the_board_come_with_the_templates_next_story(app, window, monkeypatch):
    """Keep for new stories…, then a new story from the same template: the
    Start dialog offers them, ticked, and they go on the character of the
    same name; the template's own story is left as it was."""
    from PySide6.QtWidgets import QDialog

    from sealedlore.gui.start_dialog import StartDialog
    from sealedlore.models.character import Character
    from sealedlore.models.story import Story, StorySetup
    from sealedlore.storage.template_pictures import read_kept
    from tests.test_looks_travel import png

    for number, ref in enumerate(serrik(window).reference_images):
        window.session.pictures.write(ref.file, png(30 * number + 20))
    window.session.story.setup.template = "Doomsville"
    window.show_cast_board(True, "char-serrik")
    assert not window.cast_board.keep_button.isHidden()
    monkeypatch.setattr(window, "_keep_choice", lambda *args: "keep")
    window.cast_board.keep_button.click()
    kept = read_kept("Doomsville", root=window.root)
    assert kept is not None and kept.count == 4
    window.show_cast_board(False)

    again = StoryBundle(
        story=Story(id="again", title="Doomsville again", setup=StorySetup(template="Doomsville")),
        cast=[Character(id="s2", name="Serrik Vaun")],
    )
    save_story_bundle(again, root=window.root)
    window.open_story("again")
    offered: list = []

    def start(dialog) -> int:
        offered.append(dialog.kept is not None and dialog.kept.isChecked())
        return QDialog.Accepted

    monkeypatch.setattr(StartDialog, "exec", start)
    window.start_story()
    assert offered == [True]
    card = window.session.cast[0]
    assert [(r.look, r.expression) for r in card.reference_images] == [
        ("normal clothes", "neutral"),
        ("normal clothes", "happy"),
        ("combat armour", "neutral"),
        ("", ""),
    ]
    assert window.session.pictures.read(card.reference_images[1].file) == png(50)
