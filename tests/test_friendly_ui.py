"""The window made friendlier, step two (Oct 18 2026; the author: "We have so
many features now it looks confusing for a new user", and the start page's
graphical look kept in play): the composer's More, plain words in the status
bar and above the transcript, the Prompt page under View, the People page,
and the guided new story. Offscreen, mocks only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QPushButton,
    QToolButton,
    QWidget,
)

from sealedlore.engine.budget import BudgetReport  # noqa: E402
from sealedlore.gui.composer import FEWER_LABEL, MORE_LABEL  # noqa: E402
from sealedlore.gui.main_window import MainWindow  # noqa: E402
from sealedlore.gui.new_story_guide import NewStoryGuide  # noqa: E402
from sealedlore.gui.people_page import first_sentence, people_view  # noqa: E402
from sealedlore.gui.status import StatusStrip, _short  # noqa: E402
from sealedlore.models.character import Character  # noqa: E402
from sealedlore.models.image import ImageRef  # noqa: E402
from sealedlore.models.node import Usage  # noqa: E402
from sealedlore.models.scene import OffstageCharacter, SceneState  # noqa: E402
from sealedlore.models.story import Story  # noqa: E402
from sealedlore.storage.paths import story_dir  # noqa: E402
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
    window.resize(1920, 1040)
    window.show()
    window.open_story(story.id)
    settle(app)
    yield window
    window.close()


def wait_until(app: QApplication, condition, timeout: float = 20.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        app.processEvents()
        time.sleep(0.005)


def settle(app: QApplication) -> None:
    for _ in range(10):
        app.processEvents()


def _in_window(widget: QWidget) -> QRect:
    return QRect(widget.mapTo(widget.window(), QPoint(0, 0)), widget.size())


# --- the composer's More ------------------------------------------------------------


def test_the_composer_shows_what_most_turns_need_and_more_has_the_rest(app, window):
    composer = window.composer
    # A new player meets three controls, not eight.
    assert composer.held.isVisible() and composer.speaker.isVisible()
    assert composer.length.isVisible()
    for control in (composer.scope, composer.agency, composer.private_toggle, composer.ooc_toggle):
        assert not control.isVisible()
    assert composer.more_button.text() == MORE_LABEL

    composer.more_button.click()
    settle(app)
    for control in (composer.scope, composer.agency, composer.private_toggle, composer.ooc_toggle):
        assert control.isVisible()
    assert composer.more_button.text() == FEWER_LABEL
    # Kept for every story, as the author leaves it.
    assert window.config.composer_more is True
    composer.more_button.click()
    settle(app)
    assert window.config.composer_more is False and not composer.scope.isVisible()


def test_more_opens_by_itself_for_what_a_turn_depends_on(app, window):
    composer = window.composer
    # An OOC note being written can't be put away.
    composer.set_more(True)
    composer.ooc_toggle.setChecked(True)
    composer.set_more(False)
    settle(app)
    assert composer.ooc_toggle.isVisible() and not composer.more_button.isEnabled()
    composer.ooc_toggle.setChecked(False)
    settle(app)
    assert not composer.ooc_toggle.isVisible() and composer.more_button.isEnabled()
    # Nor can a private scene's End.
    composer.set_private_state(True, "memory")
    settle(app)
    assert composer.private_toggle.isVisible() and composer.private_toggle.text() == "End private"
    composer.set_private_state(False)
    settle(app)
    assert not composer.private_toggle.isVisible()


def test_an_outcome_chosen_under_more_is_named_beside_it_when_closed(app, window):
    composer = window.composer
    composer.set_more(True)
    composer.agency.setCurrentIndex(composer.agency.findData("fiat"))
    composer.set_more(False)
    settle(app)
    assert composer.more_hint.isVisible() and composer.more_hint.text() == "Outcome: Fiat"
    composer.agency.setCurrentIndex(0)
    settle(app)
    assert not composer.more_hint.isVisible()


# --- the row above the transcript --------------------------------------------------


def test_the_row_above_the_transcript_is_never_drawn_clipped(app, window):
    """Step one put the row in a widget of its own, and it missed the branch
    bar's height changing: after a few turns its buttons were cut in half."""
    session = window.session
    session.branch_from("a0", "What if")
    window.refresh_branch_bar()
    window.reload_transcript()
    settle(app)
    assert window.branch_bar.isVisible(), "two branches: the bar names the one being read"
    row = [window.stories_button, window.view_toggle, window.find_button]
    tops = {_in_window(widget).top() for widget in row}
    assert len(tops) == 1
    # Nothing is drawn past the bottom of the widget it is in.
    for widget in row:
        assert widget.geometry().bottom() < widget.parentWidget().height()
        assert _in_window(widget).bottom() < _in_window(window.centre_stack).top()


def test_the_branch_bar_waits_for_a_second_branch(app, window):
    assert not window.branch_bar.isVisible()
    assert window.view_toggle.written.text() == "Full story"
    assert window.view_toggle.sent.text() == "What the storyteller sees"


# --- plain words ---------------------------------------------------------------------


def test_the_status_bar_says_it_in_words_and_keeps_the_numbers_in_tooltips(app):
    assert _short(950) == "950" and _short(2283) == "2.3k" and _short(60000) == "60k"
    strip = StatusStrip()
    assert strip.cost_label.text() == "" and strip.cache_label.text() == ""
    report = BudgetReport(budget=60000, total=2283, by_section={"system": 2283})
    strip.set_budget(report)
    assert strip.budget_label.text().startswith("Context 4% used (2.3k of 60k)")
    assert "2,283 of 60,000 tokens" in strip.budget_label.toolTip()
    strip.add_usage(Usage(prompt_tokens=3000, cache_read_tokens=1470, cost=0.01))
    assert strip.cache_label.text() == "Cache: 1.5k reused, 0 new"
    assert "1,470 tokens" in strip.cache_label.toolTip()
    assert strip.cost_label.text().startswith("This session $0.0100")


def test_the_session_line_says_where_to_set_up_what_is_missing(app, window):
    from sealedlore.gui.session_line import NO_ENDPOINT, NO_MODEL, describe_session

    window.session.story.defaults.main_model = None
    line, _tip = describe_session(window.session)
    assert NO_MODEL in line and NO_ENDPOINT in line
    assert "File → Settings…" in NO_MODEL


def test_the_menus_say_what_their_items_do(app, window):
    texts = {
        action.text().replace("&", "")
        for action in window.story_menu.actions()
        if not action.isSeparator()
    }
    assert {
        "Review settings with the model…",
        "Add a plot from a file…",
        "Draw a picture…",
        "Draw pictures for the cards…",
    } <= texts
    assert window.composer.image_button.text() == "Picture…"


# --- the Prompt page ------------------------------------------------------------------


def test_the_prompt_page_is_under_view_and_kept(app, window):
    tabs = window.right_tabs
    prompt = tabs.indexOf(window.inspector)
    assert not tabs.isTabVisible(prompt), "for checking the model's work, not for play"
    window.prompt_page_action.trigger()
    settle(app)
    assert tabs.isTabVisible(prompt) and tabs.currentWidget() is window.inspector
    assert window.config.prompt_page is True
    window.prompt_page_action.trigger()
    settle(app)
    assert not tabs.isTabVisible(prompt) and tabs.currentWidget() is window.people_page
    assert window.config.prompt_page is False


# --- the People page -----------------------------------------------------------------


def test_people_are_sorted_by_where_the_scene_has_them(cast):
    supporting = [
        Character(id="sup-gus", name="Gus Harlow", summary="The barkeeper. Knows everyone."),
        Character(id="sup-nell", name="Nell", summary="A courier."),
    ]
    scene = SceneState(
        present_character_ids=["char-maela", "char-serrik"],
        present_others=["Gus Harlow", "a guard"],
        offstage_but_nearby=[OffstageCharacter(character_id="char-idris", note="riding north")],
    )
    view = people_view(cast, supporting, scene, "char-serrik", missing=["Maela Orr"])
    # The author's own character first among those here, marked as theirs.
    assert [p.name for p in view.here] == ["Serrik Vaun", "Maela Orr", "Gus Harlow"]
    assert view.here[0].you and not view.here[1].you
    assert [(p.name, p.note) for p in view.away] == [("Captain Idris", "riding north")]
    assert [p.name for p in view.others] == ["Nell"]
    assert view.also_here == ["a guard"]
    assert view.here[2].line == "The barkeeper."
    assert first_sentence("x" * 200, 20) == "x" * 19 + "…"


def test_a_story_without_a_map_opens_on_its_people(app, window):
    page = window.people_page
    assert window.right_tabs.currentWidget() is page
    assert set(page.cards) == {"char-serrik", "char-maela", "char-idris"}
    assert page.cards["char-serrik"].person.you
    # A click on a card is the profile, on the Cast page.
    page.cards["char-maela"].profile_requested.emit("char-maela")
    settle(app)
    assert window.right_tabs.currentWidget() is window.cast_page
    assert window.cast_panel._current.id == "char-maela"


def test_the_people_page_offers_faces_and_a_background_until_there_are_some(
    app, window, monkeypatch
):
    page = window.people_page
    assert page.tip is not None
    # No image model: the way to one, and pictures of one's own.
    assert set(page.tip_buttons) == {"settings", "background_file"}
    opened = []
    monkeypatch.setattr(window, "open_settings", lambda tab="": opened.append(tab))
    page.tip_buttons["settings"].click()
    assert opened == ["Images"]
    # A card with no portrait says how to get one.
    card = page.cards["char-maela"]
    labels = [button.text() for button in card.findChildren(QToolButton)]
    assert "Choose a picture…" in labels
    # Not now: put away for this story.
    page._put_tip_away(window.session.story.id)
    assert page.tip is None
    window._build_people()
    assert page.tip is None


def test_a_card_with_a_portrait_shows_it_and_drops_off_the_missing(app, window):
    session = window.session
    from PySide6.QtGui import QImage

    from sealedlore.gui.image_codec import encoded

    image = QImage(30, 40, QImage.Format_RGB32)
    image.fill(0x335577)
    data = encoded(image, "PNG")
    session.pictures.write("images/refs/maela.png", data)
    maela = next(c for c in session.cast if c.id == "char-maela")
    maela.reference_images.append(ImageRef(file="images/refs/maela.png"))
    window._build_people()
    settle(app)
    view = window.people_page.view()
    assert "Maela Orr" not in view.missing
    card = window.people_page.cards["char-maela"]
    assert not card.picture.pixmap().isNull()
    assert "Draw a portrait…" not in [label for label, _ in card.menu_actions()]


def test_play_as_from_a_card(app, window):
    card = window.people_page.cards["char-maela"]
    assert "Play as Maela Orr" in [label for label, _ in card.menu_actions()]
    card.play_requested.emit("char-maela")
    settle(app)
    assert window.composer.held_character_id() == "char-maela"


# --- a story from scratch ----------------------------------------------------------------


def guide_to_characters(window, title: str = "The Long Night") -> NewStoryGuide:
    guide = NewStoryGuide(window, model="anthropic/claude-sonnet-4.6")
    window._guide = guide
    # Closed with the window at the end: kept, with no question to answer.
    guide._ask_leave = lambda: "keep"
    guide.show()
    assert not guide.skip_button.isEnabled(), "a story needs a title"
    guide.title.setText(title)
    guide.world.setPlainText("A farm in a Canadian winter, after the end of the world.")
    guide.next_button.click()
    return guide


def test_the_guide_makes_the_story_and_its_characters_on_the_cast_page(app, window):
    """The author (Oct 2026): the second step was a name and a line each for
    three characters. The story is made after the first step now, and its
    characters are made on the real Cast page, lent to the guide."""
    guide = guide_to_characters(window)
    settle(app)
    story = window.session.story
    assert guide.made and story.title == "The Long Night"
    assert story.world_bible.startswith("A farm")
    assert story.defaults.main_model == "anthropic/claude-sonnet-4.6"
    panel = window.cast_panel
    assert guide.isAncestorOf(panel), "the Cast page stands in the guide"
    # A first card, its name ready to type.
    assert [c.name for c in window.session.cast] == ["New character"]
    panel.name.setText("John")
    panel.summary.setPlainText("Ex-military, thirty, tired.")
    panel.add_character()
    panel.name.setText("Elena")
    settle(app)
    players = [guide.player.itemText(i) for i in range(guide.player.count())]
    assert players[:2] == ["John", "Elena"] and players[-1].startswith("Nobody")
    assert guide.player.currentText() == "John"
    assert panel.fill_button.isVisible()
    assert not panel.suggestions_box.isVisible(), "nothing to suggest from before the story"

    guide.next_button.click()
    guide.location.setText("The farmhouse kitchen")
    guide.opening.setPlainText("A knock at the door at dusk.")
    guide.next_button.click()
    assert guide.next_button.text() == "Create the story"
    guide.person.setCurrentIndex(guide.person.findData("first"))
    guide.tense.setCurrentIndex(guide.tense.findData("present"))
    # A length of one's own is offered here too (the author, Oct 2026).
    assert not guide.custom_length.isEnabled()
    guide.length.setCurrentIndex(guide.length.findData("custom"))
    assert guide.custom_length.isEnabled()
    guide.custom_length.setText("two tight paragraphs, dialogue-led")
    guide.length.setCurrentIndex(guide.length.findData("brief"))
    assert not guide.custom_length.isEnabled()
    guide.length.setCurrentIndex(guide.length.findData("custom"))
    assert guide.skip_button.text() == "Advanced setup…"
    guide.next_button.click()
    assert guide.result() == NewStoryGuide.Accepted and not guide.full_setup
    story = window.session.story
    assert story.style.response_style == "custom"
    assert story.style.length_target == "two tight paragraphs, dialogue-led"
    john = window.session.cast[0]
    assert [c.name for c in window.session.cast] == ["John", "Elena"]
    assert john.summary == "Ex-military, thirty, tired."
    assert story.setup.suggested_character_id == john.id
    assert story.setup.starting_scene.present_character_ids == [john.id]
    assert story.setup.starting_scene.location == "The farmhouse kitchen"
    assert story.setup.opening_text == "A knock at the door at dusk."
    assert (story.style.person, story.style.tense, story.style.response_style) == (
        "first",
        "present",
        "custom",
    )
    # The Cast page is back in the Inspector.
    window.return_cast_panel()
    assert window.cast_page.isAncestorOf(panel)
    assert not panel.suggestions_box.isHidden()
    saved = load_story_bundle(story.id, root=window.root)
    assert saved.story.setup.opening_text == "A knock at the door at dusk."
    guide.deleteLater()


def test_the_guide_can_direct_without_a_character(app, window):
    guide = guide_to_characters(window)
    guide.player.setCurrentIndex(guide.player.count() - 1)
    guide._finish()
    setup = window.session.story.setup
    assert setup.suggested_character_id is None
    assert setup.starting_scene.present_character_ids == []
    guide.deleteLater()


def test_cancelling_the_guide_keeps_or_discards_the_story_made(app, window, monkeypatch):
    guide = guide_to_characters(window, "Kept")
    kept = window.session.story.id
    guide.reject()
    assert guide.result() == NewStoryGuide.Rejected and not guide.discarded
    assert window.session.story.id == kept

    guide = guide_to_characters(window, "Discarded")
    gone = window.session.story.id
    guide._ask_leave = lambda: "discard"
    guide.reject()
    settle(app)
    assert guide.discarded and window.session is None
    assert not story_dir(gone, window.root).exists()
    assert window.cast_page.isAncestorOf(window.cast_panel)


def test_lore_characters_fill_the_guide_s_cast(app, window, monkeypatch):
    from sealedlore.gui.lore_cast_dialog import LoreCastDialog
    from sealedlore.providers.mock import MockChatProvider
    from tests.test_lore_cast import REPLY, WORLD

    guide = guide_to_characters(window)
    monkeypatch.setattr(window, "_current_provider", lambda: MockChatProvider([REPLY]))
    monkeypatch.setattr(LoreCastDialog, "exec", lambda self: QDialog.Accepted)
    monkeypatch.setattr(LoreCastDialog, "values", lambda self: (WORLD, "Maren Holt"))
    shown = []
    monkeypatch.setattr(window, "_say_lore_cast", lambda lore, result: shown.append(result))
    window.cast_panel.fill_button.click()
    wait_until(app, lambda: shown)
    names = [c.name for c in window.session.cast]
    assert "Maren Holt" in names and "Serrik Vaun" in names
    assert [c.name for c in window.session.supporting] == [
        "Old Tobin",
        "Wolf of the Keep",
        "Mrs. Holt",
    ]
    settle(app)
    players = [guide.player.itemText(i) for i in range(guide.player.count())]
    assert "Maren Holt" in players
    guide.deleteLater()


def _tab(dialog, name: str):
    return next(
        dialog.tabs.widget(i) for i in range(dialog.tabs.count()) if dialog.tabs.tabText(i) == name
    )


def test_setup_has_the_cast_page_as_its_characters_tab(app, window, monkeypatch):
    """The author (Oct 2026): Skip to the full setup… gave more detail on the
    rest, and no characters at all. Setup's Characters tab is the Cast page,
    lent, and the Starting scene tab lists whoever is made there."""
    from sealedlore.gui.setup_dialog import SetupDialog

    seen: dict = {}

    def drive(dialog) -> int:
        names = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
        assert names[:3] == ["About", "World", "Characters"]
        panel = window.cast_panel
        assert _tab(dialog, "Characters").isAncestorOf(panel)
        panel.add_character()
        panel.name.setText("Maren Holt")
        settle(app)
        dialog.tabs.setCurrentWidget(_tab(dialog, "Starting scene"))
        settle(app)
        maren = next(c for c in window.session.cast if c.name == "Maren Holt")
        seen["present"] = maren.id in dialog.presence._choices
        seen["suggested"] = [dialog.suggested.itemText(i) for i in range(dialog.suggested.count())]
        dialog.location.setText("The gatehouse")
        dialog._save()
        return SetupDialog.Accepted

    monkeypatch.setattr(SetupDialog, "exec", drive)
    offered = []
    # The test's story has begun: a new starting scene offers a new playthrough.
    monkeypatch.setattr(window, "_offer_new_playthrough", lambda: offered.append(True))
    assert window.open_setup()
    assert offered
    assert seen["present"] and "Maren Holt" in seen["suggested"]
    assert window.session.story.setup.starting_scene.location == "The gatehouse"
    assert "Maren Holt" in [c.name for c in window.session.cast]
    assert window.cast_page.isAncestorOf(window.cast_panel)


def test_skipping_to_the_full_setup_from_the_first_step_reaches_start(app, window, monkeypatch):
    from sealedlore.gui.setup_dialog import SetupDialog

    started = []
    monkeypatch.setattr(window, "start_story", lambda: started.append(window.session.story.title))

    def setup(dialog) -> int:
        window.cast_panel.add_character()
        window.cast_panel.name.setText("Ada")
        dialog._save()
        return SetupDialog.Accepted

    def guide(dialog) -> int:
        dialog.title.setText("Skipped")
        dialog.skip_button.click()
        return dialog.result()

    monkeypatch.setattr(SetupDialog, "exec", setup)
    monkeypatch.setattr(NewStoryGuide, "exec", guide)
    window.new_story()
    settle(app)
    assert [c.name for c in window.session.cast] == ["Ada"]
    assert started == ["Skipped"]


def test_new_story_runs_the_guide_then_start(app, window, monkeypatch):
    started = []
    monkeypatch.setattr(window, "start_story", lambda: started.append(window.session.story.title))

    def fill(guide) -> int:
        guide.title.setText("From Scratch")
        guide.next_button.click()
        window.cast_panel.name.setText("Ada")
        assert guide._finish()
        guide.done(NewStoryGuide.Accepted)
        return NewStoryGuide.Accepted

    monkeypatch.setattr(NewStoryGuide, "exec", fill)
    window.new_story()
    settle(app)
    assert window.session.story.title == "From Scratch"
    assert [c.name for c in window.session.cast] == ["Ada"]
    assert started == ["From Scratch"]
    assert window.cast_page.isAncestorOf(window.cast_panel)


def test_a_story_with_nobody_yet_offers_to_add_someone(app, tmp_path):
    story = Story(title="Blank")
    save_story_bundle(StoryBundle(story=story), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    try:
        window.show()
        window.open_story(story.id)
        settle(app)
        page = window.people_page
        assert window.right_tabs.currentWidget() is page and not page.cards
        page.tip_buttons["add"].click()
        settle(app)
        assert window.right_tabs.currentWidget() is window.cast_page
        assert [c.name for c in window.session.cast] == ["New character"]
    finally:
        window.close()


def test_a_story_not_begun_has_its_start_button_in_the_transcript(app, tmp_path, cast):
    story = Story(title="Unbegun")
    story.setup.opening_text = "Dusk at the farm."
    save_story_bundle(StoryBundle(story=story, cast=cast), root=tmp_path)
    window = MainWindow(root=tmp_path, use_mock=True)
    try:
        window.show()
        window.open_story(story.id)
        settle(app)
        buttons = [b.text() for b in window.transcript.findChildren(QPushButton)]
        assert "Start the story…" in buttons and "Look over the setup…" in buttons
        # And the People page lists the cast as a cast, not as who is here.
        assert not window.people_page.view().begun
    finally:
        window.close()


# --- Settings, a page at a time (Oct 18 2026) ----------------------------------------


def test_settings_pages_are_grouped_and_say_what_each_is_for(app, window):
    from sealedlore.gui.settings_dialog import PAGE_ABOUT, SettingsDialog

    dialog = SettingsDialog(window.config, window.session.story, window)
    tabs = dialog.tabs
    # The page names are kept: the manual's "Settings → Images" still finds its page.
    names = [tabs.tabText(i) for i in range(tabs.count())]
    assert names == list(PAGE_ABOUT)
    rows = [tabs.list.item(i).text() for i in range(tabs.list.count())]
    assert rows[0] == "CONNECTION" and rows.index("WRITING") < rows.index("Generation")
    tabs.setCurrentIndex(names.index("Context"))
    assert tabs.heading.text() == "Context" and tabs.about.text() == PAGE_ABOUT["Context"]
    # A click on a group heading is no page.
    heading = tabs.list.item(rows.index("PICTURES"))
    assert not heading.flags() & Qt.ItemIsSelectable
    dialog.deleteLater()


def test_what_most_never_change_is_put_away_under_a_heading(app, window):
    from sealedlore.gui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(window.config, window.session.story, window)
    dialog.resize(900, 680)
    dialog.show()
    dialog.tabs.setCurrentIndex(2)  # Generation
    settle(app)
    assert dialog.reasoning_story.isVisible() and dialog.max_tokens.isVisible()
    assert not dialog.temperature.isVisible()
    fold = next(
        b for b in dialog.tabs.currentWidget().findChildren(QToolButton) if "Sampling" in b.text()
    )
    fold.click()
    settle(app)
    assert dialog.temperature.isVisible() and "▾" in fold.text()
    # Semantic lore retrieval opens its endpoint's fields.
    dialog.tabs.setCurrentIndex(4)
    dialog.embeddings_enabled.setChecked(False)
    dialog.embeddings_fold.set_open(False)
    dialog.embeddings_enabled.setChecked(True)
    settle(app)
    assert dialog.embeddings_fold.is_open() and dialog.embed_base_url.isVisible()
    dialog.close()
    dialog.deleteLater()


def test_settings_opens_on_the_page_asked_for(app, window, monkeypatch):
    from sealedlore.gui import main_window
    from sealedlore.gui.settings_dialog import SettingsDialog

    seen = []

    class Looked(SettingsDialog):
        def exec(self) -> int:
            seen.append(self.tabs.tabText(self.tabs.currentIndex()))
            return 0

    monkeypatch.setattr(main_window, "SettingsDialog", Looked)
    window.open_settings("Images")
    assert seen == ["Images"]


# --- the panel beside the story --------------------------------------------------------


def test_the_panel_beside_the_story_is_titled_with_its_page(app, window):
    from sealedlore.gui.main_window import NO_STORY_TITLE, PAGE_ABOUT

    dock = window.inspector_dock
    assert dock.windowTitle() == f"People  ·  {PAGE_ABOUT['People']}"
    window.right_tabs.setCurrentWidget(window.cast_page)
    assert dock.windowTitle().startswith("Cast  ·  ")
    assert "Inspector" not in dock.windowTitle()
    window._close_story()
    settle(app)
    assert dock.windowTitle() == NO_STORY_TITLE
