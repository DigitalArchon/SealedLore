"""The interface tidy-up (Sept 2026): fields that read from the start, an
inspector that keeps its width, labels in the glossary's words. Offscreen,
mocks only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.models.scene import SceneState  # noqa: E402
from sealedlore.models.summary import Summary  # noqa: E402
from sealedlore.storage.repository import StoryBundle, save_story_bundle  # noqa: E402
from tests.conftest import make_exchange  # noqa: E402

LONG = "The east gate at the edge of the tree line, within sight of the Hellsville watchtower"


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
    window.resize(1366, 768)
    window.show()
    window.open_story(story.id)
    yield window
    window.close()


def needs_real_fonts() -> None:
    """Skip a test that measures text when Qt has no fonts to measure with.
    Offscreen on Windows has no font database: every glyph is a box as wide
    as the font is high, so "i" is as wide as "W" and a label ran to 297px
    where Linux's was 143px (tools/smoke/qt_metrics.py, GitHub's runner)."""
    from PySide6.QtGui import QFont, QFontMetrics

    metrics = QFontMetrics(QFont())
    if metrics.horizontalAdvance("i") == metrics.horizontalAdvance("W"):
        pytest.skip("no real fonts on this Qt platform: every glyph is a box")


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


def test_a_long_field_opens_at_its_start(app, window: MainWindow):
    scene = SceneState(location=LONG, time_of_day="Mid-morning")
    window.scene_panel.set_scene(scene, window.session.cast)
    assert window.scene_panel.location.cursorPosition() == 0
    style = window.session.story.style.model_copy(update={"prose_density": LONG})
    window.style_panel.set_style(style)
    assert window.style_panel.density.lineEdit().cursorPosition() == 0


def test_the_inspector_keeps_its_width_whatever_the_page(app, window: MainWindow):
    # "Archive 10 turns" and a long detail line once made Story so far the
    # widest page: the dock grew when it was shown and the transcript reflowed.
    summary = Summary(
        content="x",
        covered_node_ids=["u0"],
        model="anthropic/claude-sonnet-4.6",
        hand_edited=True,
        stale=True,
    )
    window.summaries_panel.set_summaries([summary], on_path_ids=set(), archivable_turns=10)
    tabs = window.right_tabs
    settle(app)
    # The minimum is what pushes the dock: it must not move with the page
    # (on the long story the dock went 413 -> 440 and never came back).
    minimums = set()
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        settle(app)
        minimums.add(tabs.minimumSizeHint().width())
    assert len(minimums) == 1, minimums


def test_direction_turns_are_labelled_direction(app, window: MainWindow):
    from sealedlore.gui.transcript import speaker_label_for
    from sealedlore.models.node import DIRECTOR_SPEAKER_ID

    assert speaker_label_for(DIRECTOR_SPEAKER_ID, window.session.cast) == "Direction"


def test_no_dialog_hides_qwidget_size(app, window: MainWindow):
    from sealedlore.gui.review_dialog import ReviewRequestDialog

    session = window.session
    dialog = ReviewRequestDialog(
        sizes=session.review_sizes(),
        model="",
        default_model=session.authoring_model,
        context_for=session.cached_context_length,
        price_for=session.cached_price,
        output_limit_for=session.cached_output_limit,
        browse=window._browse_models,
        last_complaint="",
        clear_marks=session.review_marks.clear,
        parent=window,
    )
    assert callable(dialog.size) and QWidget.size(dialog) == dialog.size()


# --- the inspector's pages and the chapters ------------------------------------------


def test_every_page_is_one_click_away_and_plot_shows_only_with_a_plot(app, window: MainWindow):
    tabs = window.right_tabs
    plot = tabs.indexOf(window.plot_panel)
    assert not tabs.isTabVisible(plot), "the story has no plot"
    shown = [i for i in range(tabs.count()) if tabs.isTabVisible(i)]
    assert len(shown) == tabs.count() - 1
    # No scrolling strip: every visible page has its own button on screen.
    settle(app)
    for index in shown:
        assert tabs._buttons[index].isVisible()
    tabs.setCurrentIndex(plot)
    assert tabs.currentIndex() != plot, "a hidden page can't be chosen"

    from sealedlore.engine.plot_md import parse_plot_markdown
    from tests.test_private import PLOT

    window.session.story.plot = parse_plot_markdown(PLOT).scenario.plot
    window._refresh_plot_panel()
    assert tabs.isTabVisible(plot)
    tabs.setCurrentIndex(plot)
    assert tabs.currentWidget() is window.plot_panel
    window.session.story.plot = None
    window._refresh_plot_panel()
    assert not tabs.isTabVisible(plot)
    assert tabs.currentWidget() is not window.plot_panel, "hiding the page leaves it"


def test_double_clicking_a_chapter_goes_to_it(app, window: MainWindow):
    on_path = Summary(content="x", covered_node_ids=["u0", "a0"])
    elsewhere = Summary(content="y", covered_node_ids=["zz"])
    panel = window.summaries_panel
    panel.set_summaries([on_path, elsewhere], on_path_ids={on_path.id}, archivable_turns=0)
    chosen: list[str] = []
    panel.chapter_chosen.connect(chosen.append)
    panel.chapters.setCurrentRow(0)
    panel.chapters.itemDoubleClicked.emit(panel.chapters.item(0))
    panel.chapters.setCurrentRow(1)
    panel.chapters.itemDoubleClicked.emit(panel.chapters.item(1))
    assert chosen == ["u0"], "only a chapter on this branch has a place to go to"
    assert not hasattr(window, "navigator"), "chapters are listed once; branches are above"


# --- the composer and the transcript ------------------------------------------------


def test_the_story_defaults_lead_with_their_value(app, window: MainWindow):
    from sealedlore.gui.composer import DEFAULT_SUFFIX

    for combo in (window.composer.length, window.composer.agency):
        first = combo.itemText(0)
        assert first.endswith(DEFAULT_SUFFIX) and not first.startswith("Story default")


def test_the_keep_selector_does_not_take_the_row(app, window: MainWindow):
    window.resize(1920, 1080)
    settle(app)
    keep = window.composer.private_keep
    widest = max(keep.fontMetrics().horizontalAdvance(keep.itemText(i)) for i in range(2))
    assert keep.width() <= widest + 60


def test_delete_is_in_the_message_menu_not_on_the_card(app, window: MainWindow):
    from sealedlore.gui.transcript import MessageWidget

    cards = [w for w in window.transcript.findChildren(MessageWidget) if w.node_id]
    assert cards
    for card in cards:
        assert "delete" in card.actions_
        assert not hasattr(card, "delete_button")


def test_the_column_stops_at_a_reading_width(app, window: MainWindow):
    from sealedlore.gui.transcript import READING_WIDTH, MessageWidget

    window.resize(1920, 1080)
    settle(app)
    cards = [w for w in window.transcript.findChildren(MessageWidget) if w.isVisible()]
    assert cards and all(card.width() <= READING_WIDTH for card in cards)


def test_every_prompt_section_has_a_plain_name():
    import sealedlore.engine.private_prompt as private_prompt
    import sealedlore.engine.prompt as prompt
    from sealedlore.gui.inspector import SECTION_LABELS

    names = {
        value
        for module in (prompt, private_prompt)
        for key, value in vars(module).items()
        if key.startswith("SECTION_") and isinstance(value, str)
    }
    assert names <= set(SECTION_LABELS), names - set(SECTION_LABELS)


# --- Settings ---------------------------------------------------------------------------


def _settings(window: MainWindow, story=None):
    from sealedlore.gui.settings_dialog import SettingsDialog

    return SettingsDialog(window.config, story, window, catalog=window.catalog)


def test_settings_tabs_and_every_model_in_one_place(app, window: MainWindow):
    from PySide6.QtWidgets import QScrollArea

    dialog = _settings(window, window.session.story)
    tabs = dialog.tabs
    names = [tabs.tabText(i) for i in range(tabs.count())]
    assert names == ["Endpoint", "Models", "Generation", "Context", "Lore", "Images", "Private"]
    assert all(isinstance(tabs.widget(i), QScrollArea) for i in range(tabs.count()))
    models = tabs.widget(names.index("Models"))
    for field in (
        dialog.model,
        dialog.summarization_model,
        dialog.authoring_model,
        dialog.scene_model,
        dialog.plot_model,
        dialog.lore_model,
        dialog.image_prompt_model,
    ):
        assert models.isAncestorOf(field)
    without_story = _settings(window)
    assert "Generation" not in [
        without_story.tabs.tabText(i) for i in range(without_story.tabs.count())
    ]
    # Every model can be chosen before any story is open: the summarisation
    # model was the story's alone, and greyed out here.
    assert without_story.summarization_model.isEnabled()


def test_the_summarisation_model_is_set_for_every_story(app, window: MainWindow):
    story = window.session.story
    assert window.session.summarization_model == window.session.model

    without_story = _settings(window)
    without_story.summarization_model.setText("every/story")
    without_story._save()
    assert window.config.summarization_model == "every/story"
    assert story.defaults.summarization_model is None
    assert window.session.summarization_model == "every/story"

    # A story's own wins, shows in the field, and an unrelated save leaves
    # both as they were.
    story.defaults.summarization_model = "this/story"
    dialog = _settings(window, story)
    assert dialog.summarization_model.text() == "this/story"
    dialog.lore_model.setText("lore/model")
    dialog._save()
    assert window.config.summarization_model == "every/story"
    assert story.defaults.summarization_model == "this/story"
    assert window.session.summarization_model == "this/story"

    # Cleared with the story open: both go back to the story model.
    dialog = _settings(window, story)
    dialog.summarization_model.setText("")
    dialog._save()
    assert window.config.summarization_model is None
    assert story.defaults.summarization_model is None
    assert window.session.summarization_model == window.session.model


def test_the_models_tab_saves_where_the_old_tabs_did(app, window: MainWindow):
    story = window.session.story
    dialog = _settings(window, story)
    dialog.summarization_model.setText("summary/model")
    dialog.lore_model.setText("lore/model")
    dialog.image_prompt_model.setText("prompt/model")
    dialog._save()
    assert story.defaults.summarization_model == "summary/model"
    assert window.config.lore_model == "lore/model"
    assert window.config.image_prompt_model == "prompt/model"


def test_recommended_models_are_offered_never_imposed(app, window: MainWindow, monkeypatch):
    """The small per-turn calls fall back to the story model when blank, the
    most expensive place for them. A new setup on NanoGPT starts with the
    recommended ones; a saved setup keeps its blanks until the button."""
    from sealedlore.gui.settings_dialog import SettingsDialog
    from sealedlore.models import config as config_module
    from sealedlore.models.config import Config, recommended_models

    table = {"scene_model": "small/scene", "plot_model": "small/plot", "lore_model": "small/lore"}
    for field, model in table.items():
        monkeypatch.setitem(config_module.RECOMMENDED_MODELS, field, model)
    assert recommended_models("https://nano-gpt.com/api/v1") == table
    assert recommended_models("https://openrouter.ai/api/v1") == {}
    assert recommended_models("https://not-nano-gpt.com/api/v1") == {}

    # A saved setup: nothing is filled in until the author asks.
    from sealedlore.models.config import ProviderConfig

    window.config.providers.append(ProviderConfig(name="default", api_key="key", model="a/model"))
    window.config.active_provider_name = "default"
    window.config.scene_model = window.config.plot_model = None
    dialog = _settings(window)
    assert dialog.scene_model.text() == "" and dialog.plot_model.text() == ""
    dialog._fill_recommended()
    assert dialog.scene_model.text() == "small/scene"
    assert dialog.plot_model.text() == "small/plot"
    assert dialog.lore_model.text() == "small/lore"
    assert window.config.scene_model is None  # not until Save
    dialog._save()
    assert window.config.scene_model == "small/scene"
    assert window.config.plot_model == "small/plot"

    # Another endpoint: the ids aren't its own, so the button says so.
    dialog = _settings(window)
    dialog.base_url.setText("https://openrouter.ai/api/v1")
    dialog.scene_model.setText("")
    dialog._fill_recommended()
    assert dialog.scene_model.text() == ""
    assert "NanoGPT" in dialog.recommended_note.text()

    # A new setup starts with them, on NanoGPT only.
    fresh = SettingsDialog(Config(), None, window, catalog=window.catalog)
    assert fresh.scene_model.text() == "small/scene"
    fresh.api_key.setText("key")
    fresh._save()
    assert fresh.config.scene_model == "small/scene"
    elsewhere = SettingsDialog(Config(), None, window, catalog=window.catalog)
    elsewhere.base_url.setText("https://openrouter.ai/api/v1")
    elsewhere._save()
    assert elsewhere.config.scene_model is None and elsewhere.config.plot_model is None


def test_dice_rolls_are_a_view_choice(app, window: MainWindow):
    from sealedlore.storage.repository import load_config

    assert window.dice_rolls_action.isChecked() == window.config.show_dice_rolls
    window.dice_rolls_action.setChecked(False)
    assert window.config.show_dice_rolls is False
    assert load_config(root=window.root).show_dice_rolls is False


# --- menus -------------------------------------------------------------------------------


def _menu(window: MainWindow, title: str):
    return next(a.menu() for a in window.menuBar().actions() if a.text().replace("&", "") == title)


def _items(menu) -> list[str]:
    return [a.text().replace("&", "") for a in menu.actions() if not a.isSeparator()]


def test_the_menus_are_grouped(app, window: MainWindow, monkeypatch):
    file_items = _items(_menu(window, "File"))
    assert [i for i in file_items if i.startswith("Import")] == ["Import…"]
    story = _menu(window, "Story")
    assert _items(window.export_menu) == [
        "Scenario…",
        "Story backup…",
        "Markdown (this branch)…",
        "Current chapter as Markdown…",
    ]
    assert "Export" in _items(story) and not any("background" in i for i in _items(story))
    assert {"Show dice rolls", "Background picture…", "Clear background"} <= set(
        _items(_menu(window, "View"))
    )
    opened: list[str] = []
    import sealedlore.gui.main_window as main_window

    monkeypatch.setattr(
        main_window.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile())
    )
    help_menu = _menu(window, "Help")
    next(a for a in help_menu.actions() if "Plot format guide" in a.text()).trigger()
    assert opened and opened[0].endswith("SampleStories")


# --- scrolling the inspector's pages --------------------------------------------------


def _wheel(widget, notches: int = -1) -> None:
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent

    local = QPointF(5, 5)
    event = QWheelEvent(
        local,
        QPointF(widget.mapToGlobal(local.toPoint())),
        QPoint(0, 0),
        QPoint(0, 120 * notches),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


def test_at_the_default_size_nothing_is_cut_off_at_the_side(app, window: MainWindow):
    from PySide6.QtWidgets import QScrollArea

    window.resize(1280, 860)  # the window's own default
    settle(app)
    tabs = window.right_tabs
    for index in range(tabs.count()):
        if not tabs.isTabVisible(index):
            continue
        tabs.setCurrentIndex(index)
        settle(app)
        for area in tabs.widget(index).findChildren(QScrollArea):
            form = area.widget()
            if form is None or area.objectName() == "transcript":
                continue
            assert form.width() <= area.viewport().width(), tabs.tabText(index)
            # The mechanism, whatever this cast's widths: the area asks for
            # its form's width, so the dock can't shrink below it (on the
            # long story the Cast form ran 46px past the edge).
            assert area.minimumSizeHint().width() >= form.minimumSizeHint().width(), tabs.tabText(
                index
            )
    assert window.minimumSizeHint().width() <= 1282


def test_the_wheel_scrolls_the_page_not_the_combo_under_it(app, window: MainWindow):
    from PySide6.QtWidgets import QComboBox, QScrollArea

    window.resize(1280, 700)
    tabs = window.right_tabs
    tabs.setCurrentWidget(window.style_panel)
    settle(app)
    area = window.style_panel.findChild(QScrollArea)
    bar = area.verticalScrollBar()
    assert bar.maximum() > 0
    combo = next(c for c in window.style_panel.findChildren(QComboBox) if c.isVisible())
    before = combo.currentIndex()
    _wheel(combo)
    settle(app)
    assert combo.currentIndex() == before, "the story's style changed under the wheel"
    assert bar.value() > 0, "the page didn't scroll"
    # Clicked into, a combo takes the wheel as usual.
    combo.setFocus()
    settle(app)
    _wheel(combo)
    settle(app)
    assert combo.currentIndex() != before or combo.count() == before + 1


# --- no story open ---------------------------------------------------------------------


def test_with_no_story_open_the_window_says_so_and_offers_a_start(app, tmp_path: Path, story, cast):
    from PySide6.QtWidgets import QPushButton

    from sealedlore.gui.composer import NO_STORY_PLACEHOLDER

    needs_real_fonts()  # the dock's width follows the forms' text
    save_story_bundle(StoryBundle(story=story, cast=cast, nodes=make_exchange(2)), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1280, 860)
    window.show()
    settle(app)
    try:
        tabs = window.right_tabs
        # Greyed forms looked usable and answered nothing, scrollbars included.
        assert tabs.is_empty_shown()
        assert not tabs.isTabVisible(tabs.indexOf(window.plot_panel))
        buttons = {b.text() for b in window.transcript.findChildren(QPushButton) if b.isVisible()}
        assert {"New story…", "Import…"} <= buttons
        assert window.composer.input.placeholderText() == NO_STORY_PLACEHOLDER
        empty_width = tabs.width()

        window.open_story(story.id)
        settle(app)
        assert not tabs.is_empty_shown()
        assert window.composer.input.placeholderText() != NO_STORY_PLACEHOLDER
        assert tabs.width() == empty_width, "opening a story doesn't move the dock"

        window._close_story()
        settle(app)
        assert tabs.is_empty_shown()
    finally:
        window.close()


def _start_screen(app, tmp_path: Path, provider=None) -> MainWindow:
    """A real (not mock) window with no story open over this config."""
    from sealedlore.models.config import Config
    from sealedlore.storage.repository import save_config

    config = Config()
    if provider is not None:
        config.providers.append(provider)
        config.active_provider_name = provider.name
    save_config(config, root=tmp_path)
    window = MainWindow(root=tmp_path)
    window.show()
    settle(app)
    return window


def _start_screen_text(window: MainWindow) -> tuple[str, set[str]]:
    from PySide6.QtWidgets import QLabel, QPushButton

    labels = [
        label.text()
        for label in window.transcript.findChildren(QLabel)
        if label.isVisible() and label.objectName() in {"welcome", "placeholder"}
    ]
    buttons = {b.text() for b in window.transcript.findChildren(QPushButton) if b.isVisible()}
    return "\n".join(labels), buttons


def test_a_first_run_is_welcomed_with_how_to_set_up(app, tmp_path: Path):
    """No endpoint: say what to set up, recommend nano-gpt with its plain link
    first and the developer's referral link second, saying what it does."""
    from sealedlore.gui.app import load_stylesheet

    before = app.styleSheet()
    app.setStyleSheet(load_stylesheet())  # unstyled, the column lays out zero high
    window = _start_screen(app, tmp_path)
    try:
        text, buttons = _start_screen_text(window)
        assert "Welcome to SealedLore" in text
        assert 'href="https://nano-gpt.com/"' in text
        assert 'href="https://nano-gpt.com/r/mackztyf"' in text
        assert text.index('href="https://nano-gpt.com/"') < text.index("/r/mackztyf")
        assert "5% discount" in text and "10%" in text
        # Only on the website: it never discounts what SealedLore uses.
        assert "directly on the NanoGPT website" in text
        assert "doesn't discount what SealedLore itself uses" in text
        assert "Still missing" not in text  # nothing is set: the steps say it all
        assert buttons == {"Open Settings…", "Import…"}
        # It opens at its heading, not followed to its last steps.
        window.resize(1366, 768)
        settle(app)
        bar = window.transcript.verticalScrollBar()
        assert bar.maximum() > 0 and bar.value() == 0
    finally:
        window.close()
        app.setStyleSheet(before)


def test_the_welcome_names_only_what_is_still_missing(app, tmp_path: Path):
    from sealedlore.models.config import ProviderConfig

    window = _start_screen(app, tmp_path, ProviderConfig(name="default", api_key="sk-test"))
    try:
        text, _ = _start_screen_text(window)
        assert "Still missing: a story model." in text
    finally:
        window.close()


def test_a_local_server_needs_no_key_to_start(app, tmp_path: Path):
    from sealedlore.models.config import ProviderConfig

    local = ProviderConfig(name="local", base_url="http://localhost:8080/v1", model="m")
    window = _start_screen(app, tmp_path, local)
    try:
        text, buttons = _start_screen_text(window)
        assert "No story open" in text
        assert "New story…" in buttons
    finally:
        window.close()


def test_saving_settings_replaces_the_welcome(app, tmp_path: Path, monkeypatch):
    from PySide6.QtCore import QEvent

    from sealedlore.gui import main_window
    from sealedlore.models.config import ProviderConfig

    class FilledIn:
        Accepted = 1

        def __init__(self, config, *args, **kwargs) -> None:
            self.config = config

        def exec(self) -> int:
            self.config.providers.append(
                ProviderConfig(name="default", api_key="sk-test", model="some/model")
            )
            self.config.active_provider_name = "default"
            return self.Accepted

    window = _start_screen(app, tmp_path)
    try:
        monkeypatch.setattr(main_window, "SettingsDialog", FilledIn)
        window.open_settings()
        # The old welcome is deleted later: flush it before looking.
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        settle(app)
        text, buttons = _start_screen_text(window)
        assert "Welcome" not in text and "No story open" in text
        assert "New story…" in buttons
    finally:
        window.close()


def test_the_prompt_copies_in_one_click(app, window: MainWindow):
    inspector = window.inspector
    window.right_tabs.setCurrentWidget(inspector)
    window._reassemble_inspector()
    settle(app)
    assert inspector.copy_text.isEnabled()
    inspector.copy_text.click()
    copied = QApplication.clipboard().text()
    assert copied.startswith("===== SYSTEM =====")
    assert "===== USER =====" in copied
    system = inspector._messages[0].text
    assert system in copied, "the whole system prompt, unaltered"
    inspector.copy_json.click()
    import json

    payload = json.loads(QApplication.clipboard().text())
    assert payload["messages"]
    assert inspector.copy_json.text() == "Copied"

    window._close_story()
    settle(app)
    assert not inspector.copy_text.isEnabled()


def test_a_new_opening_on_a_started_story_offers_a_new_playthrough(app, window, monkeypatch):
    """The opening looked like the one thing a started story couldn't change:
    saved in Setup, it did nothing. Now saving it offers a new playthrough."""
    from PySide6.QtWidgets import QDialogButtonBox, QMessageBox

    from sealedlore.gui import main_window as mw
    from sealedlore.models.scene import OffstageCharacter

    # Who is nearby at the start (a drafted story sets it) isn't on the tab,
    # and a Save used to drop it: every Save then looked like a new start.
    start = window.session.story.setup.starting_scene
    start.offstage_but_nearby = [OffstageCharacter(character_id="char-serrik", note="at the bar")]
    start.privacy = "private"

    asked: list[str] = []
    restarted: list[bool] = []
    monkeypatch.setattr(window, "restart_story", lambda: restarted.append(True))

    def answer(*args, **kwargs):
        asked.append(args[1])
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", answer)

    def run_setup(change):
        class Dialog(mw.SetupDialog):
            def exec(self):  # noqa: A003 - Qt naming
                change(self)
                self.findChild(QDialogButtonBox).accepted.emit()  # Save
                return mw.SetupDialog.Accepted

        monkeypatch.setattr(mw, "SetupDialog", Dialog)
        window.open_setup()

    run_setup(lambda d: None)  # saved unchanged
    run_setup(lambda d: d.description.setPlainText("A new blurb"))
    assert not asked, "only a change to how it starts asks"
    start = window.session.story.setup.starting_scene
    assert [o.note for o in start.offstage_but_nearby] == ["at the bar"]
    assert start.privacy == "private"

    run_setup(lambda d: d.opening.setPlainText("John is stranded; Jane comes looking."))
    assert asked == ["Start a new playthrough?"]
    assert restarted == [True]
    assert window.session.story.setup.opening_text.startswith("John is stranded")


def _roster(panel) -> dict[str, bool]:
    from PySide6.QtWidgets import QCheckBox

    return {
        panel.roster.item(row, 1).text(): panel.roster.cellWidget(row, 0)
        .findChild(QCheckBox)
        .isChecked()
        for row in range(panel.roster.rowCount())
        if panel.roster.item(row, 1) is not None  # the supporting heading
    }


def test_scene_is_first_and_lists_the_supporting_characters(app, window: MainWindow, monkeypatch):
    """Playtesting: the scene read is wrong too often to go unwatched. The long
    test story's cast is two people and the station's regulars are supporting cards,
    so the roster showed two rows and the rest was a comma list."""
    from PySide6.QtWidgets import QCheckBox

    from sealedlore.models.character import Character

    assert window.right_tabs.currentWidget() is window.scene_panel
    assert window.right_tabs.tabText(0) == "Scene"
    window.right_tabs.setCurrentWidget(window.inspector)
    window.open_story(window.session.story.id)
    assert window.right_tabs.currentWidget() is window.scene_panel, "a story opens on Scene"

    session = window.session
    hale = Character(id="sup-hale", name="Frank Hale")
    rosa = Character(id="sup-rosa", name="Rosa Delgado")
    session.supporting.extend([hale, rosa])
    session.story.scene.present_others = ["Sergeant Hale", "a gate guard"]
    session.story.scene.tracked = False
    window._refresh_scene_panel()
    panel = window.scene_panel
    rows = _roster(panel)
    assert "Serrik Vaun  · you" in rows, "the character being played is marked"
    assert rows["Frank Hale"] is True, "matched by name: 'Sergeant Hale'"
    assert rows["Rosa Delgado"] is False
    assert panel.others.text() == "a gate guard", "only people without a card"
    assert panel.roster.item(len(session.cast), 0).text() == "Supporting characters"
    assert panel.roster.verticalScrollBar().maximum() == 0, "every row shows"

    def box(name: str) -> QCheckBox:
        row = next(
            r
            for r in range(panel.roster.rowCount())
            if panel.roster.item(r, 1) is not None and panel.roster.item(r, 1).text() == name
        )
        return panel.roster.cellWidget(row, 0).findChild(QCheckBox)

    box("Rosa Delgado").setChecked(True)
    scene = session.story.scene
    assert scene.present_others == ["Sergeant Hale", "Rosa Delgado", "a gate guard"]
    assert scene.tracked, "saying who is here keeps track of everyone"
    box("Frank Hale").setChecked(False)
    assert scene.present_others == ["Rosa Delgado", "a gate guard"]

    # The plot's hidden people stay off the roster, as on the Cast page.
    monkeypatch.setattr(session, "hidden_ids", lambda *a, **k: {"sup-rosa"})
    window._refresh_scene_panel()
    assert not any(name.startswith("Rosa") for name in _roster(panel))
    assert window.minimumSizeHint().width() <= 1220


def test_the_arrows_swap_a_take_in_place(app, window: MainWindow):
    """The author's rule: a take is another version of one passage. ‹ › on an
    older passage used to switch lines, taking along what followed it."""
    from PySide6.QtWidgets import QToolButton

    from sealedlore.providers.mock import MockChatProvider

    session = window.session
    session.provider = MockChatProvider(["A second take of turn 0."])
    list(session.regenerate("a0"))
    window.reload_transcript()
    settle(app)
    retake = session.path()[1]
    widget = window.transcript.message_widget(retake.id)
    assert widget.variant_label.text() == "2 / 2"
    assert window.transcript.message_widget("u1").variant_label is None, "turns have no takes"

    back = next(b for b in widget.findChildren(QToolButton) if b.text() == "‹")
    back.click()
    settle(app)
    assert [n.id for n in session.path()] == ["u0", "a0", "u1", "a1"]
    assert session.story.active_leaf_id == "a1", "the story after it stays"


def _wait_idle(app, window: MainWindow, timeout: float = 20.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while window._busy:
        assert time.monotonic() < deadline, "the job never finished"
        app.processEvents()
        time.sleep(0.005)
    settle(app)


def test_a_branch_is_made_by_rewriting_and_the_bar_names_it(app, window: MainWindow, monkeypatch):
    """The author's rule (Sept 2026): editing an earlier message forks, and a
    branch has a name, shown above the transcript. Branch from here is gone."""
    from PySide6.QtWidgets import QInputDialog, QMessageBox

    from sealedlore.providers.mock import MockChatProvider

    session = window.session
    bar = window.branch_bar
    assert bar.button.text().startswith("Main") and bar.hint.text() == "the only one"
    widget = window.transcript.message_widget("u1")
    assert "rewrite" in widget.actions_ and "branch" not in widget.actions_

    # An author turn, rewritten: a new branch, answered at once.
    window._current_provider = lambda: MockChatProvider(["The new branch's reply."])
    widget.begin_edit(rewrite=True)
    widget._editor.setPlainText("Author turn 1, another way.")
    widget._commit_edit()
    _wait_idle(app, window)
    assert [n.content for n in session.path()][-2:] == [
        "Author turn 1, another way.",
        "The new branch's reply.",
    ]
    assert bar.button.text().startswith("Branch 2") and bar.hint.text() == "2 branches"
    start = session.path()[2]
    label = window.transcript.message_widget(start.id).branch_label
    assert label is not None and "Branch “Branch 2” starts here" in label.text()

    # Switch back through the bar; the old line is whole.
    main_action = next(a for a in bar.menu.actions() if a.text().startswith("Main"))
    main_action.trigger()
    assert [n.id for n in session.path()] == ["u0", "a0", "u1", "a1"]

    # Rename, then delete, from the bar.
    window._switch_branch(start.id)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("What if", True))
    next(a for a in bar.menu.actions() if a.text().startswith("Rename")).trigger()
    assert bar.button.text().startswith("What if")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    next(a for a in bar.menu.actions() if a.text().startswith("Delete")).trigger()
    assert bar.button.text().startswith("Main") and len(session.branch_list()) == 1
    assert session.story.active_leaf_id == "a1"


def test_rewriting_a_passage_waits_for_the_author(app, window: MainWindow):
    session = window.session
    widget = window.transcript.message_widget("a0")
    widget.begin_edit(rewrite=True)
    widget._editor.setPlainText("The author's own version.")
    widget._commit_edit()
    settle(app)
    assert not window._busy, "nothing is sent"
    assert [n.content for n in session.path()] == ["Author turn 0.", "The author's own version."]
    assert window.branch_bar.button.text().startswith("Branch 2")


def test_a_new_branch_is_named_in_its_editor(app, window: MainWindow):
    """⋯ → New branch from here… takes a name beside its Save button; blank
    is the next "Branch N", as its placeholder says."""
    session = window.session
    widget = window.transcript.message_widget("a0")
    action = widget.actions_["rewrite"]
    assert action.text() == "New branch from here…"
    action.trigger()
    assert widget._branch_name.placeholderText() == "Name (blank: Branch 2)"
    widget._editor.setPlainText("The author's own version.")
    widget._branch_name.setText("  Stay   aboard ")
    widget._commit_edit()
    settle(app)
    assert window.branch_bar.button.text().startswith("Stay aboard")
    assert session.path()[1].meta.branch_name == "Stay aboard"
    # Unchanged, from anywhere: still a branch.
    window._switch_branch(None)
    widget = window.transcript.message_widget("a1")
    widget.begin_edit(rewrite=True)
    widget._commit_edit()
    settle(app)
    assert [b.name for b in session.branch_list()] == ["Main", "Stay aboard", "Branch 3"]
    # The bar's menu tells them apart by how each begins.
    tips = {
        a.text().split("  ·")[0].strip(" ↳"): a.toolTip() for a in window.branch_bar.menu.actions()
    }
    assert tips["Stay aboard"] == "The author's own version."


def _map_window(app, window: MainWindow):
    from sealedlore.providers.mock import MockChatProvider

    session = window.session
    session.provider = MockChatProvider(["A reply on the new branch."])
    list(session.rewrite_from("u1", "Author turn 1, another way, and at some length."))
    window.refresh_branch_bar()
    window.branch_bar.map_button.click()
    settle(app)
    return session, window.story_map.view


def test_clicking_a_branch_on_the_map_moves_to_it(app, window: MainWindow):
    """The author asked for a map of every branch, and to move between them
    by clicking: a click on a lane or its name picks that branch and the map
    stays; a double-click opens the story there; Esc goes back."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    session, view = _map_window(app, window)
    branch_start = session.path()[2].id
    assert window.map_shown() and not window.composer.isVisible()
    assert [lane.branch.name for lane in view.lanes] == ["Main", "Branch 2"]
    wait = QApplication.doubleClickInterval() + 60

    # Message 2 of Main: the story goes to Main, and the map stays.
    main = view.lanes[0]
    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.point_for(main, 2))
    assert session.current_branch().start_id == branch_start, "a click waits for a double"
    QTest.qWait(wait)
    settle(app)
    assert session.current_branch().is_main and window.map_shown()
    assert view.lanes[0].branch.is_current, "redrawn with Main as the one read"
    assert window.branch_bar.button.text().startswith("Main")

    # Its name, beside the lane: that branch, at its end.
    QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=view.label_point(view.lanes[1]))
    QTest.qWait(wait)
    settle(app)
    assert session.current_branch().start_id == branch_start
    assert session.story.active_leaf_id == session.current_branch().end_id, "its end"

    # A double-click on Main's first message opens the story there.
    QTest.mouseDClick(view.viewport(), Qt.LeftButton, pos=view.point_for(view.lanes[0], 1))
    QTest.qWait(wait)
    settle(app)
    assert not window.map_shown() and window.composer.isVisible()
    assert session.current_branch().is_main
    assert not window.branch_bar.map_button.isChecked()

    window.show_map(True)
    window._on_escape()
    assert not window.map_shown()


def test_the_map_shows_how_each_branch_begins_and_its_card_stays(app, window: MainWindow):
    """Each lane is labelled with the opening of the message rewritten for
    it; hovering shows a card that stays while it is read (a tooltip went
    as soon as the pointer moved)."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QGraphicsSimpleTextItem

    from sealedlore.gui import story_map

    needs_real_fonts()  # how many words fit the gap between lanes
    _session, view = _map_window(app, window)
    texts = [i.text() for i in view.scene().items() if isinstance(i, QGraphicsSimpleTextItem)]
    assert any(t.startswith("“Author turn 1, another way") for t in texts), texts

    def move(to: QPoint) -> None:
        event = QMouseEvent(
            QMouseEvent.MouseMove,
            to,
            view.viewport().mapToGlobal(to),
            Qt.NoButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
        view.mouseMoveEvent(event)

    move(view.point_for(view.lanes[1], 4))
    card = view.card
    assert card.isVisible() and "Branch 2" in card.text()
    assert "Begins: “Author turn 1, another way" in card.text()
    move(QPoint(2, view.viewport().height() - 2))  # off every lane
    assert card.isVisible(), "it lingers once the pointer has gone"
    card._linger.setInterval(1)
    from PySide6.QtTest import QTest

    QTest.qWait(30)
    assert not card.isVisible()
    assert story_map.CARD_LINGER_MS >= 1000


def test_the_map_zooms_with_its_buttons(app, window: MainWindow):
    _session, view = _map_window(app, window)
    story_map = window.story_map
    before = view.zoom()
    story_map.zoom_in.click()
    assert view.zoom() > before
    story_map.zoom_out.click()
    story_map.zoom_out.click()
    assert view.zoom() < before
    story_map.fit_button.click()
    assert abs(view.zoom() - story_map.fit_zoom()) < 1e-6
    # A branch picked on the map keeps the zoom it was read at.
    story_map.zoom_in.click()
    kept = view.zoom()
    window._select_from_map(None, None)
    assert view.zoom() == kept


def test_the_text_size_reaches_every_part_of_the_window(app, window: MainWindow):
    """For people with poor eyesight: View → Text size scales every font the
    stylesheet sets, the application font, and the map's, and is kept."""
    from PySide6.QtCore import QEvent

    from sealedlore.gui import text_size
    from sealedlore.gui.app import load_stylesheet
    from sealedlore.storage.repository import load_config

    # A stylesheet change restyles every widget alive: earlier tests' closed
    # windows made this take a minute in the suite (half a second in the app).
    for other in app.topLevelWidgets():
        if isinstance(other, MainWindow) and other is not window:
            other.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.setStyleSheet(load_stylesheet())
    points = app.font().pointSizeF()
    try:
        window.text_size_choices[1.5].trigger()
        assert "font-size: 21px" in app.styleSheet(), "QWidget's 14px"
        assert app.font().pointSizeF() == points * 1.5
        assert text_size.points(8) == 12
        assert load_config(root=window.root).text_scale == 1.5
        assert window.text_size_choices[1.5].isChecked()
        larger = next(
            a
            for a in window.findChildren(type(window.text_size_choices[1.0]))
            if a.text() == "&Larger"
        )
        larger.trigger()
        assert window.config.text_scale == 1.75
        assert window.transcript.message_widget("a1") is not None, "rebuilt"
    finally:
        window.set_text_scale(1.0)
        app.setStyleSheet("")
    assert app.font().pointSizeF() == points


def test_a_stylesheet_scales_only_its_font_sizes():
    from sealedlore.gui.text_size import scaled_stylesheet, step

    qss = "QWidget { font-size: 14px; padding: 7px 10px; }\n#a { font-size:11px }"
    assert scaled_stylesheet(qss, 1.3) == (
        "QWidget { font-size: 18px; padding: 7px 10px; }\n#a { font-size:14px }"
    )
    assert step(1.0, 1) == 1.15 and step(1.0, -1) == 0.9 and step(2.0, 1) == 2.0


def test_regenerating_an_older_passage_in_the_window_keeps_the_story(app, window: MainWindow):
    from sealedlore.providers.mock import MockChatProvider

    window._current_provider = lambda: MockChatProvider(["A new take of turn 0."])
    window._regenerate_node("a0")
    _wait_idle(app, window)
    path = window.session.path()
    assert [n.content for n in path][1] == "A new take of turn 0."
    assert [n.id for n in path][2:] == ["u1", "a1"], "the story after it stays"
    assert window.transcript.message_widget(path[1].id).variant_label.text() == "2 / 2"
    assert len(window.session.branch_list()) == 1


def test_saving_an_edit_keeps_the_readers_place(app, tmp_path: Path, story, cast):
    """Playtesting: Save rebuilt the transcript and dropped the reader at the
    bottom, when Cancel left them where they were."""
    from PySide6.QtTest import QTest

    from sealedlore.gui.app import load_stylesheet

    bundle = StoryBundle(story=story, cast=cast, nodes=make_exchange(20))
    bundle.story.active_leaf_id = "a19"
    save_story_bundle(bundle, root=tmp_path)
    # Offscreen with no stylesheet, the column never lays its messages out
    # (all zero high); the app always has one.
    app.setStyleSheet(load_stylesheet())
    window = MainWindow(root=tmp_path, use_mock=True)
    window.resize(1366, 768)
    window.show()
    try:
        window.open_story(story.id)
        QTest.qWait(200)
        view = window.transcript
        view.scroll_to_node("a10")
        QTest.qWait(60)
        bar = view.verticalScrollBar()
        widget = view.message_widget("a10")
        offset = widget.y() - bar.value()
        assert bar.value() < bar.maximum() - 200, "the test starts away from the end"

        widget.begin_edit()
        widget._editor.setPlainText("The world answers, turn 10, corrected.")
        widget._commit_edit()
        QTest.qWait(120)
        settle(app)
        edited = view.message_widget("a10")
        assert edited is not widget and edited.body.text().endswith("corrected.")
        assert abs((edited.y() - bar.value()) - offset) <= 2, "the message stays put"
        assert bar.value() < bar.maximum() - 200
    finally:
        window.close()
        app.setStyleSheet("")


def test_a_wrap_row_keeps_one_line_while_it_fits_and_wraps_whole_groups(app):
    """Under a large text size the composer's rows held the window past a
    laptop's width; they wrap instead, a caption staying with its control."""
    from PySide6.QtWidgets import QLabel, QPushButton

    from sealedlore.gui.wrap_row import WrapRow

    host = QWidget()
    row = WrapRow(host)
    groups = []
    for name in ("One", "Two", "Three"):
        caption, button = QLabel(name), QPushButton(f"{name} button")
        for widget in (caption, button):
            widget.setFixedWidth(100)
        row.add_group([caption, button])
        groups.append((caption, button))
    host.resize(700, 200)
    host.show()
    settle(app)

    def line(widget: QWidget) -> int:
        return widget.geometry().center().y()

    assert len({line(w) for g in groups for w in g}) == 1, "one line while it fits"
    one_line = row.heightForWidth(700)

    host.resize(440, 200)
    settle(app)
    assert line(groups[0][0]) == line(groups[0][1]) == line(groups[1][1])
    assert line(groups[2][0]) == line(groups[2][1]) > line(groups[0][0]), "down a line, whole"
    assert row.heightForWidth(440) > one_line
    assert row.minimumSize().height() >= row.heightForWidth(440), "nothing below overlaps it"
    assert row.minimumSize().width() == 208, "the widest group, not the whole row"
    host.close()


def test_the_transcripts_place_asks_only_for_the_page_showing(app, window: MainWindow):
    stack = window.centre_stack
    assert stack.minimumSizeHint().height() == window.transcript.minimumSizeHint().height()
    window.show_map(True)
    assert stack.minimumSizeHint().height() == window.story_map.minimumSizeHint().height()


def test_the_prompt_tab_points_out_notes_that_say_who_plays_whom(app, window: MainWindow):
    window._reassemble_inspector()
    assert "who plays whom" not in window.inspector.summary.text()
    window.session.story.style.notes = "Keep it grim. The author now plays Serrik Vaun."
    window._reassemble_inspector()
    assert "Style notes say who plays whom (1 sentence)" in window.inspector.summary.text()


def test_setup_sets_the_style_before_the_story_begins(app, window: MainWindow, monkeypatch):
    """The author (Sept 2026): a new story's style could only be set once it had
    begun, its opening already written. Setup has the Style form; it edits a
    copy, so Cancel changes nothing, and the Style page follows a Save."""
    from PySide6.QtWidgets import QDialogButtonBox

    import sealedlore.gui.main_window as mw

    story = window.session.story

    def run_setup(change, save=True):
        class Dialog(mw.SetupDialog):
            def exec(self):  # noqa: A003 - Qt naming
                change(self)
                if save:
                    self.findChild(QDialogButtonBox).accepted.emit()
                    return mw.SetupDialog.Accepted
                return mw.SetupDialog.Rejected

        monkeypatch.setattr(mw, "SetupDialog", Dialog)
        return window.open_setup()

    def second_person(dialog):
        panel = dialog.style_panel
        panel.person.setCurrentIndex(panel.person.findData("second"))
        panel.length.setCurrentIndex(panel.length.findData("brief"))

    assert run_setup(second_person, save=False) is False
    assert story.style.person != "second", "Cancel leaves the style as it was"
    assert run_setup(second_person) is True
    assert window.session.story.style.person == "second"
    assert window.session.story.style.response_style == "brief"
    # The Style page edits the style Setup saved, not the one it replaced.
    page = window.style_panel
    page.tense.setCurrentIndex(page.tense.findData("present"))
    assert window.session.story.style.tense == "present"
    assert "Brief" in window.composer.length.itemText(0), "the story default follows"


def test_a_premise_draft_begins_only_once_setup_is_saved(app, window: MainWindow, monkeypatch):
    import sealedlore.gui.main_window as mw

    started: list[bool] = []
    monkeypatch.setattr(window, "start_story", lambda: started.append(True))
    monkeypatch.setattr(window, "_needs_provider", lambda: False)
    story_id = window.session.story.id

    class Draft:
        def build(self, **_):
            return window.session.bundle, []

    class Generate:
        Accepted = 1

        def __init__(self, *a, **k):
            self.draft = Draft()

        def exec(self):  # noqa: A003 - Qt naming
            return 1

        def model_text(self):
            return ""

    monkeypatch.setattr(mw, "GenerateDialog", Generate)
    monkeypatch.setattr(window, "open_story", lambda _id: None)
    monkeypatch.setattr(window, "open_setup", lambda: False)
    window.new_story_from_premise()
    assert started == [] and window.session.story.id == story_id
    monkeypatch.setattr(window, "open_setup", lambda: True)
    window.new_story_from_premise()
    assert started == [True]


def test_an_import_opens_setup_before_it_begins(app, window: MainWindow, tmp_path, monkeypatch):
    """The author may want changes, the style above all, before an imported
    story's opening is written: Setup first, Start only once it is saved."""
    from sealedlore.storage.scenario import scenario_from_bundle, write_scenario

    path = tmp_path / "shared.sealedlore-scenario.json"
    write_scenario(path, scenario_from_bundle(window.session.bundle, {}))
    started: list[str] = []
    monkeypatch.setattr(window, "start_story", lambda: started.append(window.session.story.id))
    before = window.session.story.id

    monkeypatch.setattr(window, "open_setup", lambda: False)
    window._import_scenario(path)
    assert window.session.story.id != before, "the import is saved and opened"
    assert started == [], "Cancel in Setup: not begun"

    monkeypatch.setattr(window, "open_setup", lambda: True)
    window._import_scenario(path)
    assert started == [window.session.story.id]


def test_person_and_tense_are_fixed_once_the_story_has_begun(app, window: MainWindow):
    """Shown greyed, for reference, with the way to a new telling. One not set
    may be set once; nothing is fixed before the first passage."""
    from sealedlore.gui.style_panel import StylePanel
    from sealedlore.models.story import StyleDirectives

    panel = StylePanel()
    panel.set_style(StyleDirectives(person="second", tense=None), started=False)
    assert panel.person.isEnabled() and panel.tense.isEnabled()
    assert panel.fixed_hint.isHidden()

    panel.set_started(True)
    assert not panel.person.isEnabled() and panel.tense.isEnabled()
    assert "Restart or Duplicate settings" in panel.fixed_hint.text()
    assert "Not set yet" in panel.fixed_hint.text()

    # Choosing the unset one doesn't lock it on the spot: a slip can be put right.
    panel.tense.setCurrentIndex(panel.tense.findData("past"))
    panel.set_started(True)  # the window asks after every job
    assert panel.tense.isEnabled()
    panel.set_style(panel._style)
    assert not panel.tense.isEnabled()

    # The window's page follows the story.
    started = bool(window.session.nodes)
    window.session.story.style.person = "third"
    window.style_panel.set_style(window.session.story.style, started=started)
    assert window.style_panel.person.isEnabled() is not started


def test_the_opening_asks_for_the_story_voice(app, window: MainWindow):
    from sealedlore.gui.setup_dialog import SetupDialog

    window.session.story.style.person = None
    window.session.story.style.tense = None
    dialog = SetupDialog(window.session.story, window.session.cast)
    assert "set them on the Style tab" in dialog.voice_hint.text()
    panel = dialog.style_panel
    panel.person.setCurrentIndex(panel.person.findData("second"))
    panel.tense.setCurrentIndex(panel.tense.findData("present"))
    assert "(second person, present tense, from the Style tab)" in dialog.voice_hint.text()
    dialog.deleteLater()


def test_the_menus_restart_opens_setup_first(app, window: MainWindow, monkeypatch):
    """Person and tense can change only in a new playthrough, so the menu's
    Restart lets them be set before its opening is written."""
    seen: list[bool] = []
    monkeypatch.setattr(window, "_open_new_bundle", lambda _b, review=False: seen.append(review))
    window.restart_action.trigger()
    window.restart_story()
    assert seen == [True, False]


def test_the_wheel_guard_reaches_controls_made_later(app, window: MainWindow):
    """The guard sits on each combo and spin box as it is polished, not on the
    whole application (every event through Python made long stories slow to
    build): a spin box made after the window, in a page that scrolls, is
    guarded too."""
    from PySide6.QtWidgets import QScrollArea, QSpinBox, QVBoxLayout

    area = QScrollArea()
    page = QWidget()
    layout = QVBoxLayout(page)
    spin = QSpinBox()
    spin.setRange(0, 100)
    spin.setValue(50)
    layout.addWidget(spin)
    layout.addSpacing(2000)
    area.setWidget(page)
    area.resize(300, 200)
    area.show()
    settle(app)
    spin.clearFocus()  # the first control takes focus as its window shows
    _wheel(spin)
    settle(app)
    assert spin.value() == 50, "the wheel changed a value it was only passing over"
    assert area.verticalScrollBar().value() > 0
    area.close()
