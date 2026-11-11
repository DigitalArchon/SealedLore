"""Bugs a review found in Your characters, the cast board's cards, the plot
editor, Setup and the new-story guide, each driven as the author meets it:
real clicks (with Ctrl and Shift) on the lists, real keys typed into the
fields."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.storage import collection  # noqa: E402
from sealedlore.storage.repository import save_story_bundle  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


# --- Your characters: the list chooses as any list does ----------------------------------


NAMES = ("Ben", "Elena", "Jane", "Marcus")


@pytest.fixture
def kept_window(app, tmp_path: Path):
    """Your characters with four of them kept, the page shown in a window."""
    root = tmp_path / "data"
    bundle = collection.load_collection(root)
    for name in NAMES:
        bundle.cast.append(collection.kept(name))
    save_story_bundle(bundle, root=root)
    window = MainWindow(root=root, use_mock=True)
    window.resize(1600, 1000)
    window.show()
    window.show_collection(True)
    settle(app)
    yield window
    window.close()


def kept_id(window: MainWindow, name: str) -> str:
    return next(c.id for c in window._kept() if c.name == name)


def click(app, page, name: str, modifier=Qt.NoModifier) -> None:
    """A real click on their row in the list, with Ctrl or Shift if given."""
    card_id = next(c.id for c in page._characters if c.name == name)
    row = next(
        row
        for row in range(page.people.count())
        if page.people.item(row).data(Qt.UserRole) == card_id
    )
    rect = page.people.visualItemRect(page.people.item(row))
    QTest.mouseClick(page.people.viewport(), Qt.LeftButton, modifier, rect.center())
    settle(app)


def chosen(page) -> list[str]:
    return sorted(page._card(card_id).name for card_id in page.chosen_ids())


def test_ctrl_click_chooses_several_as_any_list_does(app, kept_window):
    """Ctrl+click left nothing chosen, and the choice ran a click behind: the
    page re-selected the row Qt had just made current, in the middle of the
    press, so the press's own toggle undid it."""
    page = kept_window.collection_page
    click(app, page, "Elena")
    assert chosen(page) == ["Elena"]
    click(app, page, "Marcus", Qt.ControlModifier)
    assert chosen(page) == ["Elena", "Marcus"]
    assert page.title.text() == "2 characters"
    click(app, page, "Ben", Qt.ControlModifier)
    assert chosen(page) == ["Ben", "Elena", "Marcus"]
    click(app, page, "Elena", Qt.ControlModifier)
    assert chosen(page) == ["Ben", "Marcus"], "Ctrl+click on one chosen lets go of them"


def test_a_plain_click_after_several_shows_and_edits_that_one(app, kept_window):
    """Click Ben, Shift+click Jane, then click Elena: the list showed Elena
    while the title and the forms were Jane's, and what was typed went to
    Jane."""
    window = kept_window
    page = window.collection_page
    click(app, page, "Ben")
    click(app, page, "Jane", Qt.ShiftModifier)
    assert chosen(page) == ["Ben", "Elena", "Jane"]
    click(app, page, "Elena")
    assert chosen(page) == ["Elena"]
    assert page.people.currentItem().data(Qt.UserRole) == kept_id(window, "Elena")
    assert page.current == kept_id(window, "Elena")
    assert page.title.text() == "Elena"
    assert page.card_form.character is page.current_character()
    page.show_tab("Card")
    page.card_form.summary.setFocus()
    QTest.keyClicks(page.card_form.summary, "A nurse")
    window._flush_collection()
    saved = {c.name: c.summary for c in collection.load_collection(window.root).cast}
    assert saved["Elena"] == "A nurse" and not saved["Jane"]


def test_delete_after_several_asks_about_the_one_the_list_shows(app, kept_window, monkeypatch):
    """Delete acted on the card the forms still held (Jane), not the one the
    list showed (Elena)."""
    page = kept_window.collection_page
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(page, "_act", lambda what: asked.append((what, page.current)))
    click(app, page, "Ben")
    click(app, page, "Jane", Qt.ShiftModifier)
    click(app, page, "Elena")
    page.people.setFocus()
    QTest.keyClick(page.people, Qt.Key_Delete)
    assert asked == [("delete", kept_id(kept_window, "Elena"))]


def test_shift_and_arrows_choose_and_let_go_as_a_list_does(app, kept_window):
    page = kept_window.collection_page
    click(app, page, "Ben")
    page.people.setFocus()
    QTest.keyClick(page.people, Qt.Key_Down, Qt.ShiftModifier)
    settle(app)
    assert chosen(page) == ["Ben", "Elena"] and page.title.text() == "2 characters"
    QTest.keyClick(page.people, Qt.Key_Down)
    settle(app)
    assert chosen(page) == ["Jane"] and page.title.text() == "Jane"
    assert page.current == kept_id(kept_window, "Jane")


# --- Your characters: a tag for several reaches the forms of the one shown ----------------


def test_a_tag_added_to_several_is_kept_by_the_card_whose_forms_were_loaded(
    app, kept_window, monkeypatch
):
    """Elena shown, Elena and Marcus chosen, Add a tag… "winter": back on
    Elena, a key typed in her maker's notes wrote the About tab's old tags
    back over it, and "winter" was gone from the disk."""
    from PySide6.QtWidgets import QInputDialog

    window = kept_window
    page = window.collection_page
    click(app, page, "Elena")
    page.show_tab("About")
    click(app, page, "Marcus", Qt.ControlModifier)
    assert page.title.text() == "2 characters"
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("winter", True))
    page._chosen("tag")
    click(app, page, "Elena")
    assert page.title.text() == "Elena"
    assert page.about_form.tags.text() == "winter"
    page.about_form.creator_notes.setFocus()
    QTest.keyClicks(page.about_form.creator_notes, "Drawn in winter")
    window._flush_collection()
    saved = {c.name: c for c in collection.load_collection(window.root).cast}
    assert saved["Elena"].tavern.tags == ["winter"]
    assert saved["Elena"].tavern.creator_notes == "Drawn in winter"
    assert saved["Marcus"].tavern.tags == ["winter"]


def test_a_tag_taken_off_several_doesnt_come_back_on_the_card_shown(app, kept_window, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    window = kept_window
    page = window.collection_page
    for name in ("Elena", "Marcus"):
        window._kept_card(kept_id(window, name)).tavern.tags = ["survival", "road"]
    window._save_collection()
    window._refresh_collection()
    click(app, page, "Elena")
    page.show_tab("About")
    assert page.about_form.tags.text() == "survival, road"
    click(app, page, "Marcus", Qt.ControlModifier)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("survival", True))
    page._chosen("untag")
    click(app, page, "Elena")
    assert page.about_form.tags.text() == "road"
    page.about_form.creator.setFocus()
    QTest.keyClicks(page.about_form.creator, "me")
    window._flush_collection()
    saved = {c.name: c for c in collection.load_collection(window.root).cast}
    assert saved["Elena"].tavern.tags == ["road"] and saved["Elena"].tavern.creator == "me"


# --- the plot editor: a name typed a key at a time renames only its own item --------------


SAMPLE = Path(__file__).resolve().parents[1] / "SampleStories" / "Doomsville.md"


@pytest.fixture
def plot_editor(app):
    from sealedlore.gui.plot_editor import PlotEditorWindow

    window = PlotEditorWindow()
    assert window.open_path(SAMPLE)
    window.show()
    yield window
    window.modified = False
    window.close()


def type_name(app, field, text: str) -> None:
    """What a person does: the field chosen whole, then the keys."""
    field.setFocus()
    field.selectAll()
    QTest.keyClicks(field, text)
    settle(app)


def brought(document) -> list[list[str]]:
    return [list(way.brings) for event in document.events for way in (event, *event.variants)]


def test_a_new_characters_name_typed_through_anothers_leaves_them_be(app, plot_editor):
    """Typing "Elena Ruiz Junior" for a new character renamed Elena Ruiz, and
    every event that brings her, on the way (at "Elena Ruiz", then " ")."""
    from sealedlore.gui.plot_editor import CHARACTER, HEADER

    editor = plot_editor
    names = [c.name for c in editor.document.characters]
    brings = brought(editor.document)
    editor._select((HEADER, CHARACTER, -1))
    editor._add()
    type_name(app, editor.pages.currentWidget().name, "Elena Ruiz Junior")
    assert [c.name for c in editor.document.characters] == [*names, "Elena Ruiz Junior"]
    assert brought(editor.document) == brings
    elena = names.index("Elena Ruiz")
    assert editor._find((CHARACTER, elena, -1)).text(0) == "Elena Ruiz"
    assert editor._find((CHARACTER, len(names), -1)).text(0).startswith("Elena Ruiz Junior")
    editor.check_now()
    assert not [p for p in editor.problems if p.error]


def test_a_new_facts_name_typed_through_anothers_leaves_its_conditions_be(app, plot_editor):
    from sealedlore.gui.plot_editor import FACT, HEADER

    editor = plot_editor
    facts = [f.name for f in editor.document.facts]
    editor._select((HEADER, FACT, -1))
    editor._add()
    type_name(app, editor.pages.currentWidget().name, "hellsville_two")
    assert [f.name for f in editor.document.facts] == [*facts, "hellsville_two"]
    fall = editor.document.events[0]
    assert fall.requires[0].fact == "hellsville"
    assert fall.variants[0].sets[0].fact == "hellsville"


def test_two_new_events_one_retitled_leaves_the_other(app, plot_editor):
    from sealedlore.gui.plot_editor import HEADER, OPTIONAL

    editor = plot_editor
    editor._select((HEADER, OPTIONAL, -1))
    editor._add()
    editor._add()
    type_name(app, editor.pages.currentWidget().title, "The second one")
    titles = [e.title for e in editor.document.events]
    assert titles[-2:] == ["New event", "The second one"]


def test_a_name_emptied_and_typed_anew_keeps_what_named_it(app, plot_editor):
    """Marcus Webb backspaced to nothing, then "Helena Ruiz": the events that
    brought him brought "" (saved as nobody)."""
    from sealedlore.gui.plot_editor import CHARACTER

    editor = plot_editor
    marcus = [c.name for c in editor.document.characters].index("Marcus Webb")
    editor._select((CHARACTER, marcus, -1))
    field = editor.pages.currentWidget().name
    field.setFocus()
    field.end(False)
    for _ in "Marcus Webb":
        QTest.keyClick(field, Qt.Key_Backspace)
    assert "Marcus Webb" in [n for names in brought(editor.document) for n in names], (
        "while blank, what named him keeps his name"
    )
    QTest.keyClicks(field, "Helena Ruiz")
    settle(app)
    named = [n for names in brought(editor.document) for n in names]
    assert "Helena Ruiz" in named and "Marcus Webb" not in named and "" not in named
    editor.check_now()
    assert "- brings: Helena Ruiz, Elena Ruiz" in editor.rendered.text
    assert not [p for p in editor.problems if p.error]


def test_a_name_typed_onto_anothers_and_on_keeps_both_apart(app, plot_editor):
    """Ben renamed by typing "Marcus Webb, the elder": while his name is
    Marcus Webb's, nothing of Marcus's follows him, and his own references
    end on his new name."""
    from sealedlore.gui.plot_editor import CHARACTER

    editor = plot_editor
    names = [c.name for c in editor.document.characters]
    editor._select((CHARACTER, names.index("Ben"), -1))
    field = editor.pages.currentWidget().name
    type_name(app, field, "Marcus Webb")
    assert [c.name for c in editor.document.characters].count("Marcus Webb") == 2
    caves = next(e for e in editor.document.events if e.title == "Ben in the caves")
    assert caves.brings == ["Ben"], "on another's name, what named him keeps his own"
    editor.check_now()
    shown = [p.message for p in editor.problems]
    assert "Two characters are called “Marcus Webb”: what names them can't tell them apart." in (
        shown
    )
    QTest.keyClicks(field, " the elder")
    settle(app)
    assert caves.brings == ["Marcus Webb the elder"]
    assert ["Marcus Webb", "Elena Ruiz"] in brought(editor.document)


def test_an_ordinary_rename_still_follows_as_it_is_typed(app, plot_editor):
    from sealedlore.gui.plot_editor import CHARACTER

    editor = plot_editor
    names = [c.name for c in editor.document.characters]
    editor._select((CHARACTER, names.index("Elena Ruiz"), -1))
    type_name(app, editor.pages.currentWidget().name, "Elena Cruz")
    named = [n for names in brought(editor.document) for n in names]
    assert "Elena Cruz" in named and "Elena Ruiz" not in named
    editor.check_now()
    assert not [p for p in editor.problems if p.error]


# --- the cast board's card: a name retyped renames the rosters once ------------------------


@pytest.fixture
def story_window(app, tmp_path: Path, story, cast):
    """The small test story, two exchanges in, with Sergeant Hale and Sergeant
    Price (supporting) in every scene and the starting one."""
    from sealedlore.models.character import Character
    from sealedlore.models.supporting import SupportingCast
    from sealedlore.storage.repository import StoryBundle
    from tests.conftest import make_exchange

    nodes = make_exchange(2)
    bundle = StoryBundle(
        story=story,
        cast=list(cast),
        nodes=nodes,
        supporting=SupportingCast(
            characters=[
                Character(id="sup-hale", name="Sergeant Hale", summary="A watch sergeant."),
                Character(id="sup-price", name="Sergeant Price", summary="Another."),
            ]
        ),
    )
    bundle.story.active_leaf_id = nodes[-1].id
    bundle.story.held_character_id = "char-serrik"
    for scene in (bundle.story.scene, bundle.story.setup.starting_scene):
        scene.present_others = ["Sergeant Hale", "Sergeant Price"]
        scene.tracked = True
    nodes[1].meta.scene = bundle.story.scene.model_copy(deep=True)
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1600, 1000)
    window.show()
    window.open_story(story.id)
    settle(app)
    yield window
    window.close()


def rosters(session) -> list[list[str]]:
    scenes = [*session._all_scenes(), session.story.setup.starting_scene]
    return [list(scene.present_others) for scene in scenes]


def open_card(app, window, card_id: str):
    window.show_cast_page(card_id)
    settle(app)
    panel = window.cast_panel
    panel._select(card_id)
    panel.name.setFocus()
    return panel


def test_a_name_emptied_and_retyped_keeps_its_place_on_every_roster(app, story_window):
    """Sergeant Hale's name backspaced to nothing and retyped "Corporal Dunn"
    left "S" on every roster: each key renamed them from the key before, and
    nothing was renamed from a blank name."""
    window = story_window
    session = window.session
    panel = open_card(app, window, "sup-hale")
    panel.name.end(False)
    for _ in "Sergeant Hale":
        QTest.keyClick(panel.name, Qt.Key_Backspace)
    hale = next(c for c in session.supporting if c.id == "sup-hale")
    assert hale.name == "Sergeant Hale", "a blank name isn't kept"
    QTest.keyClicks(panel.name, "Corporal Dunn")
    QTest.keyClick(panel.name, Qt.Key_Return)
    settle(app)
    assert hale.name == "Corporal Dunn"
    assert all(roster == ["Corporal Dunn", "Sergeant Price"] for roster in rosters(session))


def test_a_name_typed_through_anothers_leaves_them_on_the_rosters(app, story_window):
    """Hale chosen whole and retyped "Sergeant Price Jr": at "Sergeant Price"
    he took the real Sergeant Price's entry, and the rosters ended with one
    man where there were two."""
    window = story_window
    session = window.session
    panel = open_card(app, window, "sup-hale")
    panel.name.selectAll()
    QTest.keyClicks(panel.name, "Sergeant Price Jr")
    window.show_cast_board(False)
    settle(app)
    assert all(roster == ["Sergeant Price Jr", "Sergeant Price"] for roster in rosters(session))
    from sealedlore.storage.repository import load_story_bundle

    saved = load_story_bundle(session.story.id, root=window.root)
    assert saved.story.scene.present_others == ["Sergeant Price Jr", "Sergeant Price"]


def test_a_cast_members_name_left_blank_is_never_kept(app, story_window):
    """A cast member's name backspaced away was saved as "" and the composer's
    Playing list showed an empty entry."""
    window = story_window
    panel = open_card(app, window, "char-maela")
    panel.name.end(False)
    for _ in "Maela Orr":
        QTest.keyClick(panel.name, Qt.Key_Backspace)
    window.show_cast_board(False)
    settle(app)
    assert panel.name.text() == "Maela Orr", "the field shows the name the card kept"
    from sealedlore.storage.repository import load_story_bundle

    saved = load_story_bundle(window.session.story.id, root=window.root)
    assert [c.name for c in saved.cast] == ["Serrik Vaun", "Maela Orr", "Captain Idris"]
    held = window.composer.held
    assert "" not in [held.itemText(i).strip() for i in range(held.count())]


# --- a character removed takes their id with them ----------------------------------------


def answer_questions(monkeypatch, answer) -> list[str]:
    """Every QMessageBox.question answered `answer`; their titles kept."""
    from PySide6.QtWidgets import QMessageBox

    asked: list[str] = []

    def question(_parent, title, *_args, **_kwargs):
        asked.append(title)
        return answer

    monkeypatch.setattr(QMessageBox, "question", question)
    return asked


def run_setup(window, monkeypatch, drive) -> bool:
    """Story → Setup…, `drive` doing what the author does in it."""
    from sealedlore.gui.setup_dialog import SetupDialog

    def exec_(dialog):
        dialog.show()
        dialog._remember_opened()
        settle(QApplication.instance())
        return drive(dialog)

    monkeypatch.setattr(SetupDialog, "exec", exec_)
    monkeypatch.setattr(window, "_offer_new_playthrough", lambda: None)
    return window.open_setup()


def remove_on_the_tab(app, window, dialog, card_id: str) -> None:
    tabs = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    dialog.tabs.setCurrentIndex(tabs.index("Characters"))
    settle(app)
    window.cast_panel._select(card_id)
    window.cast_panel.remove_button.click()
    settle(app)


def test_characters_removed_in_setup_leave_its_starting_scene(app, story_window, monkeypatch):
    """Maela and Idris taken out on Setup's Characters tab, then Save: the
    starting scene still had Maela present and Idris pinned elsewhere, and
    Maela was still the character suggested."""
    from PySide6.QtWidgets import QDialog, QMessageBox

    window = story_window
    session = window.session
    setup = session.story.setup
    setup.starting_scene.present_character_ids = ["char-serrik", "char-maela"]
    setup.absent_at_start = ["char-idris"]
    setup.suggested_character_id = "char-maela"
    session.save()
    answer_questions(monkeypatch, QMessageBox.Yes)

    def drive(dialog):
        remove_on_the_tab(app, window, dialog, "char-maela")
        remove_on_the_tab(app, window, dialog, "char-idris")
        dialog._save()
        return QDialog.Accepted

    assert run_setup(window, monkeypatch, drive)
    assert [c.id for c in session.cast] == ["char-serrik"]
    setup = session.story.setup
    assert setup.starting_scene.present_character_ids == ["char-serrik"]
    assert setup.absent_at_start == [] and setup.suggested_character_id is None
    assert "char-maela" not in session.story.scene.present_character_ids
    from sealedlore.storage.repository import load_story_bundle

    saved = load_story_bundle(session.story.id, root=window.root)
    assert saved.story.setup.starting_scene.present_character_ids == ["char-serrik"]
    assert all(
        "char-maela" not in node.meta.scene.present_character_ids
        for node in saved.nodes
        if node.meta.scene is not None
    )


def test_a_character_removed_in_setup_is_not_a_change_cancel_asks_about(
    app, story_window, monkeypatch
):
    """Cards on the Characters tab are kept as they are made; a card taken
    out there is not something of Setup's that Cancel would throw away."""
    from PySide6.QtWidgets import QDialog, QMessageBox

    window = story_window
    session = window.session
    session.story.setup.starting_scene.present_character_ids = ["char-serrik", "char-maela"]
    session.save()
    asked = answer_questions(monkeypatch, QMessageBox.Yes)

    def drive(dialog):
        remove_on_the_tab(app, window, dialog, "char-maela")
        dialog.reject()
        return QDialog.Rejected

    assert not run_setup(window, monkeypatch, drive)
    assert asked == ["Remove this character?"]
    assert session.story.setup.starting_scene.present_character_ids == ["char-serrik"]


def test_removing_the_character_played_lets_go_of_them(app, story_window, monkeypatch):
    """Serrik, the one played, removed on the cast board: the story still
    held him, and the scene still had him present."""
    from PySide6.QtWidgets import QMessageBox

    window = story_window
    session = window.session
    answer_questions(monkeypatch, QMessageBox.Yes)
    window.show_cast_page("char-serrik")
    settle(app)
    window.cast_panel._select("char-serrik")
    window.cast_panel.remove_button.click()
    settle(app)
    assert [c.id for c in session.cast] == ["char-maela", "char-idris"]
    assert session.story.held_character_id is None
    assert "char-serrik" not in session.story.scene.present_character_ids
    window.show_cast_board(False)
    settle(app)
    from sealedlore.storage.repository import load_story_bundle

    saved = load_story_bundle(session.story.id, root=window.root)
    assert saved.story.held_character_id is None
    snapshots = [node.meta.scene for node in saved.nodes if node.meta.scene is not None]
    assert snapshots and all("char-serrik" not in s.present_character_ids for s in snapshots)


def test_a_supporting_card_removed_leaves_the_rosters(app, story_window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = story_window
    session = window.session
    answer_questions(monkeypatch, QMessageBox.Yes)
    window.show_cast_page("sup-hale")
    settle(app)
    window.cast_panel._select("sup-hale")
    window.cast_panel.remove_button.click()
    settle(app)
    assert [c.id for c in session.supporting] == ["sup-price"]
    assert all(roster == ["Sergeant Price"] for roster in rosters(session))


# --- the new-story guide ------------------------------------------------------------------


@pytest.fixture
def guide(app, story_window):
    from sealedlore.gui.new_story_guide import NewStoryGuide

    guide = NewStoryGuide(story_window)
    story_window._guide = guide
    guide.show()
    settle(app)
    yield guide
    guide.made, guide.discarded = True, True  # closed with nothing to ask
    guide.done(0)
    story_window._guide = None
    story_window.return_cast_panel()
    guide.deleteLater()


def test_the_guide_asks_before_esc_loses_what_its_first_step_holds(app, guide, monkeypatch):
    """Esc on the first step, a title and a long world typed: closed with no
    question, and everything typed was gone."""
    from PySide6.QtWidgets import QMessageBox

    asked: list[tuple[str, object]] = []

    def question(_parent, title, _text, _buttons, default):
        asked.append((title, default))
        return QMessageBox.Cancel

    monkeypatch.setattr(QMessageBox, "question", question)
    guide.title.setFocus()
    QTest.keyClicks(guide.title, "The Long Night")
    guide.world.setPlainText("A farm in a Canadian winter, after the end of the world. " * 20)
    QTest.keyClick(guide.world, Qt.Key_Escape)
    settle(app)
    assert asked == [("Discard what you wrote?", QMessageBox.Cancel)]
    assert guide.isVisible() and not guide.made
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
    QTest.keyClick(guide.world, Qt.Key_Escape)
    settle(app)
    assert not guide.isVisible()


def test_nothing_typed_in_the_guide_closes_it_without_a_question(app, guide):
    QTest.keyClick(guide.title, Qt.Key_Escape)
    settle(app)
    assert not guide.isVisible()


def to_characters(app, guide) -> None:
    guide.title.setText("The Long Night")
    guide.next_button.click()
    settle(app)


def test_back_on_the_characters_step_with_nobody_adds_no_card(
    app, guide, story_window, monkeypatch
):
    """The first card taken out, Nobody chosen to play, on to the opening and
    back: a stray "New character" was added again."""
    from PySide6.QtWidgets import QMessageBox

    to_characters(app, guide)
    session = story_window.session
    assert [c.name for c in session.cast] == ["New character"]
    answer_questions(monkeypatch, QMessageBox.Yes)
    story_window.cast_panel.remove_button.click()
    settle(app)
    assert session.cast == []
    guide.player.setCurrentIndex(guide.player.count() - 1)
    guide.next_button.click()
    guide.back_button.click()
    settle(app)
    assert guide.stack.currentIndex() == 1 and session.cast == []


def test_enter_in_the_guides_card_doesnt_move_to_the_next_step(app, guide, story_window):
    """Enter in the lent card's Name pressed Next."""
    to_characters(app, guide)
    panel = story_window.cast_panel
    panel.name.setFocus()
    panel.name.selectAll()
    QTest.keyClicks(panel.name, "John")
    QTest.keyClick(panel.name, Qt.Key_Return)
    settle(app)
    assert guide.stack.currentIndex() == 1
    assert [c.name for c in story_window.session.cast] == ["John"]
