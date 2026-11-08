"""The look-over of the main window (Nov 2026): pages that left room empty
or squeezed what mattered, and readouts that said nothing. Offscreen, mocks
only."""

from __future__ import annotations

import gc
import os
import re
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QFormLayout, QWidget  # noqa: E402

from sealedlore.engine.retrieval import RetrievalReport  # noqa: E402
from sealedlore.gui.lore_panel import LorePanel  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.sizing import GrowingText  # noqa: E402
from sealedlore.gui.start_presence import StartPresence  # noqa: E402
from sealedlore.gui.status import BudgetBar, ElidingLabel  # noqa: E402
from sealedlore.gui.style_panel import StylePanel  # noqa: E402
from sealedlore.gui.summaries_panel import SummariesPanel  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.lore import LoreEntry  # noqa: E402
from sealedlore.models.summary import Summary  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402
from tests.test_ui_tidy import needs_real_fonts  # noqa: E402


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


def test_the_style_pages_two_forms_line_their_fields_up(app):
    """Length and Perspective are a form of their own (they stay live when
    the block is hand-edited); its fields began 35px left of the rest."""
    needs_real_fonts()
    panel = StylePanel()
    panel.resize(700, 1400)
    panel.show()
    settle(app)
    length = panel.length.mapTo(panel, panel.length.rect().topLeft()).x()
    person = panel.person.mapTo(panel, panel.person.rect().topLeft()).x()
    assert length == person
    panel.close()


def test_the_hint_sits_under_whos_there_not_in_the_middle_of_the_room(app, cast):
    """Given more room than its capped list needed, the hint label took it
    and drew its text centred in it: alone in the middle of Setup's tab and
    the Start dialog."""
    presence = StartPresence(cast, max_height=60)
    presence.resize(600, 700)
    presence.show()
    settle(app)
    hint = next(w for w in presence.findChildren(QWidget) if w.objectName() == "hintLabel")
    assert hint.height() <= hint.heightForWidth(hint.width()) + 4
    presence.close()


def test_an_empty_box_is_as_tall_as_its_placeholder_wraps(app):
    """A one-line box whose placeholder wrapped showed half its second line
    (Setup's Characters tab, Personality)."""
    host = QWidget()
    form = QFormLayout(host)
    box = GrowingText(lines=1)
    box.setPlaceholderText("Optional: a few words, as a SillyTavern card has them. " * 3)
    form.addRow("Personality", box)
    host.resize(320, 400)
    host.show()
    settle(app)
    line = box.fontMetrics().lineSpacing()
    assert box.viewport().height() >= 2 * line
    box.setPlainText("Wry.")
    settle(app)
    assert box.viewport().height() < 2 * line
    host.close()


def test_the_context_meter_is_short_and_away_with_no_story(app):
    bar = BudgetBar()
    assert bar.isHidden()
    assert bar.maximumWidth() == BudgetBar.MAX_WIDTH


def test_an_empty_readout_takes_no_room(app):
    label = ElidingLabel("")
    assert label.sizeHint().width() == 0
    assert label.minimumSizeHint().width() == 0
    label.setText("Lore: all 2 sent")
    assert label.sizeHint().width() > 0


def test_the_lore_list_shows_its_entries_and_an_empty_report_is_a_line(app):
    """At fixed shares the list showed two entries and a half whatever the
    lorebook held, and an empty report a third of the page."""
    panel = LorePanel()
    panel.set_entries([LoreEntry(title=f"Place {n}", content="x") for n in range(6)])
    panel.resize(700, 1000)
    panel.show()
    settle(app)
    rows = panel.list.viewport().height() // panel.list.sizeHintForRow(0)
    assert rows >= 6
    assert panel.injected.isHidden()
    report = panel._splitter.sizes()[2]
    assert report < 120

    entry = LoreEntry(title="The Accord", content="y")
    panel.show_retrieval(RetrievalReport(mode="whole", standing=(entry,)))
    settle(app)
    assert not panel.injected.isHidden()
    panel.close()


def test_story_so_far_with_nothing_archived_says_so_and_no_more(app):
    panel = SummariesPanel()
    panel.show()
    panel.set_summaries([], on_path_ids=set(), archivable_turns=0)
    assert panel.chapters.isHidden() and panel.editor.isHidden()
    assert panel.rebuild_button.isHidden() and panel.save_button.isHidden()
    assert not panel.archive_button.isHidden()
    assert "once the oldest turns are archived" in panel.detail.text()

    chapter = Summary(content="They met at the gate.", covered_node_ids=["u1", "a1"])
    panel.set_summaries([chapter], on_path_ids={chapter.id}, archivable_turns=0)
    assert not panel.chapters.isHidden() and not panel.editor.isHidden()
    assert not panel.rebuild_button.isHidden()
    panel.close()


@pytest.fixture
def window(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(2))
    bundle.story.active_leaf_id = "a1"
    bundle.story.held_character_id = "char-serrik"
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    yield window
    window.close()


def test_the_boards_count_of_gaps_is_the_fill_buttons(window):
    """It said "1 gap to draw" beside "Fill every gap (5)…": a new look's
    waiting cells weren't counted."""
    session = window.session
    serrik = next(c for c in session.cast if c.id == "char-serrik")
    session.pictures.write("images/refs/p.png", b"picture")
    serrik.reference_images = [
        ImageRef(id="p", file="images/refs/p.png", look="normal clothes", expression="neutral")
    ]
    serrik.sheet_looks = ["injured"]
    window.show_cast_board(True, "char-serrik")
    board = window.cast_board
    counted = re.search(r"(\d+) gaps? to draw", board.stats.text())
    filled = re.search(r"Fill every gap \((\d+)\)", board.fill_button.text())
    assert counted and filled
    assert counted.group(1) == filled.group(1)
    window.show_cast_board(False)


def test_a_story_not_begun_has_room_under_its_buttons(app, tmp_path: Path, story, cast):
    bundle = StoryBundle(story=story, cast=cast)
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(story.id)
    box = next(w for w in window.findChildren(QWidget) if w.objectName() == "placeholderBox")
    assert box.layout().contentsMargins().bottom() > 0
    window.close()


def test_the_plot_page_says_nothing_has_happened_rather_than_show_an_empty_table(
    app, tmp_path: Path
):
    from sealedlore.engine.plot_md import parse_plot_markdown
    from sealedlore.storage.scenario import bundle_from_scenario

    sample = Path(__file__).resolve().parents[1] / "SampleStories" / "Doomsville.md"
    bundle = bundle_from_scenario(parse_plot_markdown(sample.read_text(encoding="utf-8")).scenario)
    save_story_bundle(bundle, root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.open_story(bundle.story.id)
    panel = window.plot_panel
    assert not panel.events.isVisibleTo(panel)
    assert panel.events_label.text().startswith("Nothing has happened yet")
    panel.spoilers.setChecked(True)
    assert panel.events.isVisibleTo(panel)
    assert panel.events_label.text() == "Events"
    window.close()


def test_the_portraits_more_menu_outlives_setup_borrowing_the_card(app, tmp_path, monkeypatch):
    """The card lent to Setup and back keeps its More menu. In a run of the
    real window its actions, made by addAction(text), were deleted with the
    dialog and the next story opened raised; that run's conditions aren't
    reproduced here, so this checks the path, not that case."""
    from PySide6.QtCore import QCoreApplication, QEvent

    from sealedlore.engine.plot_md import parse_plot_markdown
    from sealedlore.gui.ref_images import load_plot_pictures
    from sealedlore.gui.setup_dialog import SetupDialog
    from sealedlore.storage.scenario import bundle_from_scenario

    sample = Path(__file__).resolve().parents[1] / "SampleStories" / "The Silverwood.md"
    parsed = parse_plot_markdown(sample.read_text(encoding="utf-8"))
    assert load_plot_pictures(parsed, sample.parent) == []
    bundle = bundle_from_scenario(parsed.scenario)
    save_story_bundle(bundle, root=tmp_path)
    from sealedlore.gui.text_size import load_stylesheet

    app.setStyleSheet(load_stylesheet())
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1920, 1080)
    window.show()
    window.open_story(bundle.story.id)
    settle(app)

    def looked_over(dialog: SetupDialog) -> int:
        dialog.show()
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            settle(app)
            dialog.grab()
        dialog.hide()
        return SetupDialog.Rejected

    monkeypatch.setattr(SetupDialog, "exec", looked_over)
    window.open_setup()
    for _ in range(5):
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        settle(app)
    gc.collect()
    portrait = window.cast_panel.portrait
    # Each still there (a deleted one raises), and the story opens again.
    for action in (portrait.again_action, portrait.add_action, portrait.remove_action):
        assert action.text()
    window.open_story(bundle.story.id)
    window.close()
    app.setStyleSheet("")
