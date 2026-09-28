"""The plot file editor, offscreen: what it writes is what the importer reads."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sealedlore.engine.plot_md import parse_plot_markdown  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.plot_editor import (  # noqa: E402
    CHARACTER,
    EVENT,
    FACT,
    HEADER,
    OPTIONAL,
    REF_ROLE,
    TIMELINE,
    PlotEditorWindow,
)
from sealedlore.gui.plot_editor_pages import WayPage, condition_key  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "SampleStories" / "Doomsville.md"


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def editor(app) -> PlotEditorWindow:
    window = PlotEditorWindow()
    assert window.open_path(SAMPLE)
    return window


def rows(page: WayPage) -> list:
    return [page.requires.rows.itemAt(i).widget() for i in range(page.requires.rows.count())]


def test_a_sample_opens_clean_and_every_page_fills(app, editor: PlotEditorWindow):
    assert editor.problems == []
    assert not editor.modified
    assert editor.windowTitle().startswith("Doomsville.md")
    stack = [editor.tree.topLevelItem(i) for i in range(editor.tree.topLevelItemCount())]
    seen = 0
    while stack:
        item = stack.pop()
        stack.extend(item.child(i) for i in range(item.childCount()))
        editor.tree.setCurrentItem(item)
        app.processEvents()
        seen += 1
    # 9 headers, 6 characters, 2 places, 3 facts, 6 events, 3 variants.
    assert seen == 29

    editor._select((EVENT, 0, 1))
    page = editor.pages.currentWidget()
    assert isinstance(page, WayPage) and not page.is_event
    assert page.title.text() == "The player's character has been, but is away"
    kinds = [(row.kind.currentText(), row.first.currentText()) for row in rows(page)]
    assert kinds == [("Has visited", "Hellsville"), ("Is not at place", "Hellsville")]
    ticked = [
        page.brings.item(i).data(Qt.UserRole)
        for i in range(page.brings.count())
        if page.brings.item(i).checkState() == Qt.Checked
    ]
    assert ticked == ["Elena Ruiz"]
    # Nothing about looking changed the document.
    assert not editor.modified


def test_renaming_a_fact_follows_into_the_events(app, editor: PlotEditorWindow):
    editor._select((FACT, 0, -1))
    editor.pages.currentWidget().name.setText("town")
    editor.check_now()
    assert editor.problems == []
    assert editor.modified
    fall = editor.document.events[0]
    assert fall.requires[0].fact == "town"
    assert fall.variants[0].sets[0].fact == "town"
    assert "- requires: town = standing" in editor.rendered.text


def test_conditions_are_built_not_typed(app, editor: PlotEditorWindow):
    editor._select((HEADER, CHARACTER, -1))
    editor._add()
    character = editor.pages.currentWidget()
    character.name.setText("Zed")
    character.role.setCurrentIndex(character.role.findData("player"))

    editor._select((HEADER, OPTIONAL, -1))
    editor._add()
    page = editor.pages.currentWidget()
    assert isinstance(page, WayPage) and page.is_event
    page.title.setText("Zed arrives")
    page.requires._add()
    page.requires._add()
    page.requires._add()
    at, fact, happened = rows(page)
    at.kind.setCurrentIndex(at.kind.findData(condition_key("at", False)))
    at.who.setCurrentIndex(at.who.findData("Zed"))
    at.first.setCurrentIndex(at.first.findData("Hellsville"))
    fact.first.setCurrentIndex(fact.first.findData("has_companions"))
    fact.second.setCurrentIndex(fact.second.findData("yes"))
    happened.kind.setCurrentIndex(happened.kind.findData(condition_key("happened", True)))
    happened.first.setCurrentIndex(happened.first.findData("The caves"))
    page.sets._add()
    assignment = page.sets.rows.itemAt(0).widget()
    assignment.fact.setCurrentIndex(assignment.fact.findData("elena"))
    assignment.value.setCurrentIndex(assignment.value.findData("dead"))
    page.brings.item(0).setCheckState(Qt.Checked)
    page.tell.setPlainText("Zed walks in.")

    editor.check_now()
    assert editor.problems == []
    text = editor.rendered.text
    assert "- requires: Zed at = Hellsville, has_companions = yes, The caves not happened" in text
    assert "- sets: elena = dead" in text
    assert "- brings: Ben" in text
    zed = parse_plot_markdown(text).scenario.plot.event("zed_arrives")
    assert [c.place_test if c.place else c.fact or c.event_id for c in zed.requires] == [
        "at",
        "has_companions",
        "the_caves",
    ]


def test_a_problem_names_its_item_and_selects_it(app, editor: PlotEditorWindow):
    editor._select((EVENT, 5, -1))
    page = editor.pages.currentWidget()
    row = rows(page)[0]
    row.kind.setCurrentIndex(row.kind.findData("text"))
    row.text.setText("the moon rises!")
    editor.check_now()
    assert [p.where for p in editor.problems] == [(EVENT, 5, -1)]
    listed = editor.problem_list.item(0).text()
    assert listed.startswith("Error — Event “Ben in the caves”: Can't read the condition")
    assert editor.summary.text().startswith("1 error stops this file importing")
    assert editor.tree.currentItem().foreground(0).color().name() == "#c0392b"

    editor._select((HEADER, CHARACTER, -1))
    editor._on_problem_activated(editor.problem_list.item(0))
    assert editor.current_ref() == (EVENT, 5, -1)


def test_ticking_the_timeline_moves_the_event(app, editor: PlotEditorWindow):
    index = next(i for i, e in enumerate(editor.document.events) if not e.timeline)
    editor._select((EVENT, index, -1))
    page = editor.pages.currentWidget()
    assert editor.tree.currentItem().parent().data(0, REF_ROLE) == (HEADER, OPTIONAL, -1)
    page.timeline.setChecked(True)
    app.processEvents()
    assert editor.tree.currentItem().parent().data(0, REF_ROLE) == (HEADER, TIMELINE, -1)
    assert editor.document.events[index].timeline
    editor.check_now()
    assert editor.problems == []


def test_saving_writes_what_reopens_and_imports(app, editor: PlotEditorWindow, tmp_path: Path):
    editor._select((HEADER, CHARACTER, -1))
    editor._add()
    editor.pages.currentWidget().name.setText("Zed")
    path = tmp_path / "out.md"
    assert editor._write(path)
    assert not editor.modified and editor.path == path

    again = PlotEditorWindow()
    assert again.open_path(path)
    assert again.problems == []
    assert [c.name for c in again.document.characters][-1] == "Zed"
    parsed = parse_plot_markdown(path.read_text(encoding="utf-8"))
    assert parsed.ok and "Zed" in {c.name for c in parsed.scenario.cast}


def test_discarding_on_close_really_discards(app, tmp_path: Path, monkeypatch):
    """The window is kept while the app runs: closed with Discard and opened
    again, it showed the changes that had just been thrown away."""
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
    path = tmp_path / "Doomsville.md"
    path.write_text(SAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    editor = PlotEditorWindow()
    assert editor.open_path(path)
    saved = [c.name for c in editor.document.characters]
    editor.show()

    editor._select((HEADER, CHARACTER, -1))
    editor._add()
    editor.pages.currentWidget().name.setText("Zed")
    assert editor.modified
    assert editor.close()

    editor.show()
    assert [c.name for c in editor.document.characters] == saved
    assert not editor.modified and editor.path == path
    assert editor.windowTitle().startswith("Doomsville.md —")
    assert path.read_text(encoding="utf-8") == SAMPLE.read_text(encoding="utf-8")
    assert editor.close()

    # A file never saved goes back to a blank one.
    untitled = PlotEditorWindow()
    untitled.show()
    untitled._select((HEADER, CHARACTER, -1))
    untitled._add()
    assert untitled.modified and untitled.close()
    untitled.show()
    assert untitled.document.characters == [] and not untitled.modified
    assert untitled.close()


EXTENDED = Path(__file__).resolve().parent / "data" / "doomsville_extended.md"
PICTURES = EXTENDED.parent / "pictures"


def copied_plot(folder: Path) -> Path:
    """The extended plot and its pictures folder, where a test may write."""
    import shutil

    folder.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PICTURES, folder / "pictures")
    path = folder / "Doomsville.md"
    path.write_text(EXTENDED.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def test_the_editor_shows_adds_and_removes_an_items_pictures(app, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from sealedlore.storage.image_meta import picture_kind

    path = copied_plot(tmp_path / "story")
    editor = PlotEditorWindow()
    assert editor.open_path(path)
    assert editor.problems == []

    editor._select((CHARACTER, 0, -1))
    strip = editor.pages.currentWidget().pictures
    assert [strip.list.item(i).text() for i in range(strip.list.count())] == [
        "in her army coat, rifle slung"
    ]
    assert not strip.list.item(0).icon().isNull()
    assert not editor.modified

    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(PICTURES / "janes-truck.png")], "")
    )
    strip._add()
    jane = editor.document.characters[0]
    assert len(jane.pictures) == 2 and editor.modified
    added = jane.pictures[1].file
    assert added.startswith("pictures/jane-moss-") and added.endswith(".png")
    assert picture_kind((path.parent / added).read_bytes()) == ".png"
    # The original is where it was: a copy went into the folder.
    assert (PICTURES / "janes-truck.png").is_file()

    strip.list.setCurrentRow(0)
    strip._remove()
    assert [p.file for p in jane.pictures] == [added]
    assert (path.parent / "pictures" / "jane-moss.png").is_file()  # the file stays

    # A picture whose file has gone says so, and the importer leaves it out.
    (path.parent / added).unlink()
    editor._select((CHARACTER, 0, -1))
    assert strip.list.item(0).text().endswith("(missing)")
    assert editor.save()
    assert f"- picture: {added}" in path.read_text(encoding="utf-8")
    editor.close()


def test_an_unsaved_plot_is_saved_before_it_takes_a_picture(app, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    editor = PlotEditorWindow()
    editor._select((HEADER, CHARACTER, -1))
    editor._add()
    strip = editor.pages.currentWidget().pictures
    target = tmp_path / "new" / "Mine.md"
    asked: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: asked.append(a[1]) or QMessageBox.Save
    )
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    monkeypatch.setattr(
        QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(PICTURES / "jane-moss.png")], "")
    )
    strip._add()
    assert "Save the plot file first" in asked
    assert editor.path == target and target.is_file()
    pictures = editor.document.characters[0].pictures
    assert len(pictures) == 1 and (target.parent / pictures[0].file).is_file()
    assert editor.save()
    editor.close()


def test_save_as_elsewhere_takes_the_pictures_along(app, tmp_path: Path):
    path = copied_plot(tmp_path / "here")
    editor = PlotEditorWindow()
    assert editor.open_path(path)
    there = tmp_path / "there" / "Copy.md"
    assert editor._write(there)
    assert sorted(p.name for p in (there.parent / "pictures").iterdir()) == [
        "hellsville-gate.png",
        "jane-moss.png",
        "janes-truck.png",
    ]
    editor._select((CHARACTER, 0, -1))
    assert not editor.pages.currentWidget().pictures.list.item(0).icon().isNull()
    editor.close()


def test_importing_a_plot_brings_its_pictures_onto_the_cards(app, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from sealedlore.storage.images import story_path

    path = copied_plot(tmp_path / "story")
    (path.parent / "pictures" / "janes-truck.png").unlink()
    shown: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "exec", lambda box: shown.append(box.detailedText()) or QMessageBox.Yes
    )
    window = MainWindow(root=tmp_path / "data", use_mock=True)
    parsed = window._read_plot_file(path)
    assert parsed is not None and parsed.ok

    # The one that has gone is a note the author sees, never a stop.
    assert len(shown) == 1
    assert "Jane's truck's picture “pictures/janes-truck.png” was left out" in shown[0]

    scenario = parsed.scenario
    jane = next(c for c in scenario.cast if c.name == "Jane Moss")
    town = next(entry for entry in scenario.lore if entry.title == "Hellsville")
    truck = next(entry for entry in scenario.lore if entry.title == "Jane's truck")
    assert [ref.caption for ref in jane.reference_images] == ["in her army coat, rifle slung"]
    assert len(town.reference_images) == 2 and truck.reference_images == []
    files = [ref.file for ref in (*jane.reference_images, *town.reference_images)]
    assert sorted(scenario.reference_files) == sorted(files)
    assert all(file.startswith("images/refs/") for file in files)

    from sealedlore.storage.repository import save_story_bundle
    from sealedlore.storage.scenario import bundle_from_scenario

    bundle = bundle_from_scenario(scenario)
    save_story_bundle(bundle, root=tmp_path / "data")
    for file in files:
        assert story_path(bundle.story.id, file, tmp_path / "data").is_file()
    assert bundle.cast[0].reference_images[0].file == jane.reference_images[0].file
    window.close()


def test_a_picture_line_cannot_reach_outside_the_plots_folder(tmp_path: Path):
    """The parser refuses such a path; the loader would too, link or not."""
    from sealedlore.gui.ref_images import inside

    folder = tmp_path / "story"
    (folder / "pictures").mkdir(parents=True)
    secret = tmp_path / "secret.png"
    secret.write_bytes((PICTURES / "jane-moss.png").read_bytes())
    assert inside(folder, "pictures/jane.png") == (folder / "pictures" / "jane.png").resolve()
    assert inside(folder, "../secret.png") is None
    assert inside(folder, str(secret)) is None
    assert inside(folder, "") is None
    try:
        (folder / "pictures" / "link.png").symlink_to(secret)
    except OSError:  # Windows, without the right to make links
        return
    assert inside(folder, "pictures/link.png") is None


def test_the_main_window_opens_the_editor_and_starts_a_story(app, tmp_path: Path):
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_plot_editor()
    editor = window._plot_editor
    assert editor is not None and editor.isVisible()
    window.open_plot_editor()
    assert window._plot_editor is editor

    assert editor.open_path(SAMPLE)
    editor.path = tmp_path / "Doomsville.md"
    # Starting a story asks who to play; the test just closes that.
    QTimer.singleShot(0, lambda: QApplication.activeModalWidget().reject())
    editor.start_story()
    app.processEvents()
    assert window.session is not None
    assert window.session.story.title == "Doomsville"
    assert window.session.story.plot is not None
    editor.close()
    window.close()


def test_quitting_the_app_asks_about_the_editors_unsaved_file(app, tmp_path: Path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_plot_editor()
    editor = window._plot_editor
    editor._select((HEADER, CHARACTER, -1))
    editor._add()
    assert editor.modified

    answers = [QMessageBox.Cancel, QMessageBox.Discard]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answers.pop(0))
    # Cancel the editor's prompt: the app must stay open.
    assert not window.close()
    assert editor.isVisible()
    # Discard it: both close.
    assert window.close()
    assert not editor.isVisible()
    assert answers == []
